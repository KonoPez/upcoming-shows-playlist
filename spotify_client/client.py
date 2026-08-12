"""
High-level Spotify operations: playlist management, user history, discography fetching.
"""

import logging
import re
import time
from collections import defaultdict
from typing import NamedTuple, Optional

import spotipy

from cache import Cache
from sources.models import Track, normalize_track_name

logger = logging.getLogger(__name__)

ARTIST_TRACKS_TTL = 30 * 24 * 3600   # 30 days — discographies don't change fast
ARTIST_NAME_TTL = 90 * 24 * 3600     # 90 days — canonical names change about as often as IDs
FAMILIARITY_TTL = 6 * 3600            # 6 hours — top tracks don't shift meaningfully intra-day
RECENTLY_PLAYED_TTL = 5 * 60          # 5 min — deduplicates the two callers within a single run
API_DELAY = 0.1                        # 100 ms between calls
MAX_ALBUMS_PER_ARTIST = 25            # most recent studio albums + singles

# Album names that indicate the entire album is a non-studio recording.
_VARIANT_ALBUM_RE = re.compile(
    r'\b(live\s+(at|from|in)\b'
    r'|unplugged'
    r'|acoustic\s+sessions?'
    r'|demos?'
    r'|remixed|(?:the\s+)?remixes?)\b'
    r'|\(live\)',
    re.IGNORECASE,
)

# Track name patterns that mark a variant recording:
#   Parenthetical: "Song (Live)", "Song (X Remix)", "Song (Acoustic Version)"
#   Dash suffix:   "Song - Acoustic", "Song - Demo", "Song - Live Version"
# Parenthetical form requires parens so "Live Wire" or "Acoustic" as a title is unaffected.
# Dash form requires the keyword to start immediately after the dash so "Song - A Demo
# of Courage" would not match, but in practice that pattern doesn't occur on Spotify.
_VARIANT_TRACK_RE = re.compile(
    r'\(.*\b(live|acoustic|unplugged|remix|instrumental|demo|a\s*cappella|acapella)\b.*\)'
    r'|\s+[-–]\s+(live|acoustic|unplugged|remix|instrumental|demo|a\s*cappella|acapella)\b',
    re.IGNORECASE,
)

# Which release a song should be taken from when it appears on several.  The
# album version is the canonical one: pre-release singles are frequently
# different mixes/edits, and a song's presence on the album is what the artist
# treats as the finished record.  Compilations sit in the middle — usually the
# album master, but a second-hand pressing.  Unknown types sort last.
# Note Spotify types EPs as 'single', so EPs rank alongside singles.
_ALBUM_TYPE_RANK = {'album': 3, 'compilation': 2, 'single': 1}


def album_type_rank(album_type: str) -> int:
    """Higher is better. See _ALBUM_TYPE_RANK."""
    return _ALBUM_TYPE_RANK.get((album_type or '').strip().lower(), 0)


def pad_release_date(release_date: str) -> str:
    """
    Pad a Spotify release_date to YYYY-MM-DD so string comparison is chronological.

    Spotify returns year-, month-, or day-precision dates.  Compared raw, "2020"
    sorts before "2020-01-01" even though they denote the same release, so any
    "which came first" test needs the padded form.
    """
    rd = release_date or '0000'
    if len(rd) == 4:
        return rd + '-01-01'
    if len(rd) == 7:
        return rd + '-01'
    return rd


def deduplicate_tracks(
    tracks: list[Track],
    lastfm_scores: Optional[dict] = None,
) -> list[Track]:
    """
    Group tracks by base song name and return one representative per group.

    Within each group the representative is chosen by priority:
      1. Exactly one non-variant exists → use it.
      2. Otherwise (multiple non-variants, or all variants) → the best release
         type: album over compilation over single, so the album cut of a song
         beats the pre-release single that shares its name.
      3. Tied on release type → highest Last.fm score. Note this looks Last.fm
         up by the *literal* track name, not the normalized group key, so it
         only distinguishes candidates in the rare case where a candidate's
         exact title is itself a Last.fm top track (e.g. a well-known
         live/variant recording when no studio version exists). When candidates
         share a base name they score equally here.
      4. Last.fm unavailable or all tied (the common case) → shortest track title.
      5. Still tied → first in the list (albums are newest-first, so this
         naturally favours the more recent pressing).
    """
    groups: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        groups[normalize_track_name(track.name)].append(track)

    result: list[Track] = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        non_variants = [
            t for t in group
            if not bool(_VARIANT_ALBUM_RE.search(t.album_name) or _VARIANT_TRACK_RE.search(t.name))
        ]

        if len(non_variants) == 1:
            result.append(non_variants[0])
            continue

        # Prefer non-variants when multiple exist; fall back to all if none exist.
        candidates = non_variants if non_variants else group

        # Album cut beats single/EP cut. Applied before the weaker tiebreaks
        # below, which can't tell an album version from a pre-release single.
        best_rank = max(album_type_rank(t.album_type) for t in candidates)
        candidates = [t for t in candidates if album_type_rank(t.album_type) == best_rank]

        if lastfm_scores and len(candidates) > 1:
            best_score = max(lastfm_scores.get(t.name.lower().strip(), 0.0) for t in candidates)
            if best_score > 0.0:
                result.append(max(candidates, key=lambda t: lastfm_scores.get(t.name.lower().strip(), 0.0)))
                continue

        result.append(min(candidates, key=lambda t: len(t.name)))

    return result


class ArtistTopScore(NamedTuple):
    name: str
    score: float


class SpotifyClient:
    def __init__(self, sp: spotipy.Spotify):
        self.sp = sp
        self._user_id: Optional[str] = None

    @property
    def user_id(self) -> str:
        if not self._user_id:
            self._user_id = self.sp.current_user()['id']
        return self._user_id

    # ── Playlist management ──────────────────────────────────────────────────

    def get_or_create_playlist(
        self,
        name: str,
        playlist_id: Optional[str] = None,
        description: str = 'Auto-updated: tracks to prep for upcoming concerts',
    ) -> str:
        """
        Return the ID of the managed playlist.
        Uses the existing playlist_id if still valid; otherwise creates a new one.
        """
        if playlist_id:
            try:
                pl = self.sp.playlist(playlist_id, fields='id,name')
                logger.info(f'Using playlist: "{pl["name"]}" ({pl["id"]})')
                return pl['id']
            except Exception:
                logger.warning(
                    f'Playlist {playlist_id} not found or inaccessible — creating a new one.'
                )

        # POST /me/playlists is the correct endpoint for creating a playlist
        # for the authenticated user. spotipy's user_playlist_create() uses
        # POST /users/{id}/playlists which is for creating playlists on behalf
        # of *other* users and requires different permissions.
        pl = self.sp._post('me/playlists', payload={
            'name': name,
            'public': False,
            'description': description,
        })
        logger.info(f'Created playlist: "{name}" ({pl["id"]})')
        return pl['id']

    def get_playlist_artists(self, playlist_id: str) -> list[tuple[str, str]]:
        """
        Return unique (artist_id, artist_name) pairs for every track in a playlist.
        Only the primary (first) artist per track is included.

        Uses sp.playlist_tracks() rather than sp._get() — the latter requires
        additional_types=track to be set explicitly, otherwise Spotify returns 403.
        """
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        offset = 0
        while True:
            try:
                resp = self.sp.playlist_tracks(playlist_id, limit=100, offset=offset)
            except Exception as e:
                logger.warning(f'Failed to fetch playlist items: {e}')
                break
            for item in resp.get('items', []):
                # Spotify's playlist tracks endpoint returns the track object
                # under the key 'item' (not 'track') in its current API version.
                # 'track' in the response is a boolean field, not the object.
                track = (item or {}).get('item') or {}
                artists = track.get('artists', [])
                if artists:
                    primary = artists[0]
                    aid  = primary.get('id')
                    name = primary.get('name', '')
                    if aid and aid not in seen:
                        seen.add(aid)
                        result.append((aid, name))
            if resp.get('next') is None:
                break
            offset += 100
            time.sleep(API_DELAY)
        return result

    def update_playlist_description(self, playlist_id: str, description: str) -> None:
        """Update the description field of an existing playlist."""
        self.sp._put(f'playlists/{playlist_id}', payload={'description': description})

    def update_playlist_tracks(self, playlist_id: str, track_uris: list[str]) -> None:
        """
        Atomically replace the playlist's contents.
        Handles Spotify's 100-URI-per-call limit.
        """
        if not track_uris:
            logger.warning('No tracks to add — playlist not modified.')
            return

        # Replace existing tracks with the first batch (clears + adds in one call)
        self.sp.playlist_replace_items(playlist_id, track_uris[:100])

        # Append any overflow (target is 60, so this is a safety net)
        for i in range(100, len(track_uris), 100):
            batch = track_uris[i:i + 100]
            self.sp.playlist_add_items(playlist_id, batch)
            time.sleep(API_DELAY)

        logger.info(f'Playlist updated with {len(track_uris)} tracks.')

    # ── User listening history ────────────────────────────────────────────────

    def _get_recently_played_raw(self, cache: Cache) -> list[Track]:
        """
        Fetch the raw recently-played items from Spotify, cached for
        RECENTLY_PLAYED_TTL so the two callers within a single run share one call.
        """
        cache_key = 'recently_played_raw'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            result = self.sp.current_user_recently_played(limit=50)
            items = result.get('items', [])
            cache.set(cache_key, items, RECENTLY_PLAYED_TTL)
            return items
        except Exception as e:
            logger.warning(f'Failed to fetch recently played: {e}')
            return []

    def get_user_familiarity(self, cache: Cache) -> dict[str, float]:
        """
        Build a {track_id: familiarity_score} map from Spotify's API signals.
        Result is cached for FAMILIARITY_TTL (6 h) — top-track lists don't
        shift meaningfully within a day, and this avoids 4 API calls on re-runs.

        Scores:
          short_term top tracks (≈4 weeks)   → 1.0
          medium_term top tracks (≈6 months) → 0.8
          long_term top tracks (years)        → 0.6
          recently played                     → 0.5
        Tracks in multiple lists get the highest score.
        """
        cache_key = 'user_familiarity'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        familiarity: dict[str, float] = {}

        for time_range, score in [
            ('short_term', 1.0),
            ('medium_term', 0.8),
            ('long_term', 0.6),
        ]:
            try:
                result = self.sp.current_user_top_tracks(limit=50, time_range=time_range)
                for track in result.get('items', []):
                    tid = track.get('id')
                    if tid:
                        familiarity[tid] = max(familiarity.get(tid, 0.0), score)
                time.sleep(API_DELAY)
            except Exception as e:
                logger.warning(f'Failed to fetch top tracks ({time_range}): {e}')

        for item in self._get_recently_played_raw(cache):
            tid = item.get('track', {}).get('id')
            if tid:
                familiarity[tid] = max(familiarity.get(tid, 0.0), 0.5)

        cache.set(cache_key, familiarity, FAMILIARITY_TTL)
        return familiarity

    def get_artist_top_scores(self, cache: Cache) -> dict[str, ArtistTopScore]:
        """
        Build a {artist_id: ArtistTopScore(name, score)} map from Spotify's
        top-artists API.  Mirrors get_user_familiarity but at the artist level.

        Scores:
          short_term top artists (≈4 weeks)   → 1.0
          medium_term top artists (≈6 months) → 0.8
          long_term top artists (years)        → 0.6
        Artists in multiple lists get the highest score.
        Cached for FAMILIARITY_TTL (6 h).
        """
        cache_key = 'artist_top_scores_v2'
        cached = cache.get(cache_key)
        if cached is not None:
            # NamedTuples round-trip through JSON as lists; reconstruct
            return {
                aid: ArtistTopScore(*v) if isinstance(v, list) else v
                for aid, v in cached.items()
            }

        scores: dict[str, ArtistTopScore] = {}
        for time_range, score in [
            ('short_term', 1.0),
            ('medium_term', 0.8),
            ('long_term', 0.6),
        ]:
            try:
                result = self.sp.current_user_top_artists(limit=50, time_range=time_range)
                for artist in result.get('items', []):
                    aid  = artist.get('id')
                    name = artist.get('name', '')
                    if aid and aid not in scores:
                        scores[aid] = ArtistTopScore(name=name, score=score)
                time.sleep(API_DELAY)
            except Exception as e:
                logger.warning(f'Failed to fetch top artists ({time_range}): {e}')

        cache.set(cache_key, scores, FAMILIARITY_TTL)
        return scores

    def get_recently_played_with_artists(self, cache: Cache) -> list[Track]:
        """
        Return play events for local history accumulation.
        Each entry: {track_id, artist_id, played_at}.
        Reuses the recently-played response cached by _get_recently_played_raw.
        """
        plays: list[dict] = []
        for item in self._get_recently_played_raw(cache):
            track = item.get('track', {})
            track_id = track.get('id')
            played_at = item.get('played_at', '')
            artists = track.get('artists', [])
            if track_id and played_at and artists:
                plays.append({
                    'track_id': track_id,
                    'artist_id': artists[0].get('id', ''),
                    'played_at': played_at,
                })
        return plays

    def get_artist_names(self, artist_ids: list[str], cache: Cache) -> dict[str, str]:
        """
        Return {artist_id: canonical Spotify name} for the given IDs.

        Calendar titles spell artists however the venue felt like it, and the
        third-party APIs (setlist.fm, Last.fm) index by the canonical name, so
        downstream lookups need this rather than the string we searched with.

        One request per artist — the batch `artists?ids=` endpoint returns 403
        for PKCE apps.  Each name is cached for ARTIST_NAME_TTL, so this costs
        a call only for artists seen for the first time in 90 days.
        """
        names: dict[str, str] = {}

        for artist_id in artist_ids:
            cache_key = f'artist_name:{artist_id}'
            cached = cache.get(cache_key)
            if cached is not None:
                names[artist_id] = cached
                continue

            try:
                name = (self.sp.artist(artist_id) or {}).get('name')
                time.sleep(API_DELAY)
            except Exception as e:
                logger.debug(f'Failed to fetch canonical name for {artist_id}: {e}')
                continue

            if name:
                names[artist_id] = name
                cache.set(cache_key, name, ARTIST_NAME_TTL)

        return names

    # ── Artist discography ────────────────────────────────────────────────────

    def get_artist_tracks(self, artist_id: str, cache: Cache) -> list[Track]:
        """Return tracks for an artist's discography. Cached for ARTIST_TRACKS_TTL."""
        # v2: entries cached before album_type existed picked the single's copy
        # of a song over the album's, so they must be refetched rather than aged out.
        cache_key = f'artist_tracks:v2:{artist_id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return [Track.from_dict(t) for t in cached]

        tracks = self._fetch_artist_tracks(artist_id)
        if tracks:
            cache.set(cache_key, [t.to_dict() for t in tracks], ARTIST_TRACKS_TTL)
        return tracks

    def _is_variant_recording(self, track_name: str, album_name: str) -> bool:
        """Return True if the track is a live, acoustic, remix, instrumental, or demo version."""
        return bool(
            _VARIANT_ALBUM_RE.search(album_name)
            or _VARIANT_TRACK_RE.search(track_name)
        )

    def _fetch_artist_tracks(self, artist_id: str) -> list[Track]:
        albums = self._get_artist_albums(artist_id)
        if not albums:
            return []

        # Collect studio and variant tracks separately, each deduped by base name.
        # Within a category the album cut wins over a single/EP cut; between two
        # releases of the same type the OLDEST wins, so a later re-recording
        # can't silently overwrite the original or inflate its recency score.
        # deduplicate_tracks() then picks between the two categories.
        studio_by_name: dict[str, Track] = {}   # base_name → best non-variant
        variant_by_name: dict[str, Track] = {}  # base_name → best variant

        def is_better(candidate: Track, incumbent: Track) -> bool:
            cand_rank = album_type_rank(candidate.album_type)
            inc_rank = album_type_rank(incumbent.album_type)
            if cand_rank != inc_rank:
                return cand_rank > inc_rank
            return pad_release_date(candidate.release_date) < pad_release_date(
                incumbent.release_date
            )

        for album in albums:
            album_id = album['id']
            album_name = album.get('name', '')
            release_date = album.get('release_date', '')
            release_date_precision = album.get('release_date_precision', 'year')
            album_type = album.get('album_type', '')

            for track in self._get_album_tracks(album_id):
                tid = track.get('id')
                if not tid:
                    continue

                track_name = track.get('name', '')
                is_variant = self._is_variant_recording(track_name, album_name)
                base = normalize_track_name(track_name)
                target = variant_by_name if is_variant else studio_by_name

                candidate = Track(
                    id=tid,
                    name=track_name,
                    duration_ms=track.get('duration_ms', 0),
                    album_id=album_id,
                    album_name=album_name,
                    release_date=release_date,
                    release_date_precision=release_date_precision,
                    album_type=album_type,
                )

                existing = target.get(base)
                if existing and not is_better(candidate, existing):
                    continue

                target[base] = candidate

        all_tracks = list(studio_by_name.values()) + list(variant_by_name.values())
        if not all_tracks:
            return []

        logger.debug(
            f'Artist {artist_id}: {len(studio_by_name)} studio + '
            f'{len(variant_by_name)} variant tracks from {len(albums)} albums'
        )
        return all_tracks

    def _get_artist_albums(self, artist_id: str) -> list[dict]:
        """Return the most recent MAX_ALBUMS_PER_ARTIST albums + singles (raw album dicts)."""
        import logging as _logging
        _spotipy_logger = _logging.getLogger('spotipy.client')
        _orig_level = _spotipy_logger.level
        # Suppress spotipy's ERROR logs for the entire method — we handle all
        # errors ourselves (400 limit retries log at DEBUG, others at WARNING).
        _spotipy_logger.setLevel(_logging.CRITICAL)

        albums: list[dict] = []
        offset = 0
        limit = 20

        try:
            while len(albums) < MAX_ALBUMS_PER_ARTIST * 2:  # over-fetch, then trim
                try:
                    # Embed params in the URL string rather than passing as kwargs.
                    # When kwargs are passed, requests.urlencode encodes the comma in
                    # "album,single" as %2C, which Spotify rejects with a 400 error.
                    # An inline query string is left unmodified by requests.
                    result = self.sp._get(
                        f'artists/{artist_id}/albums'
                        f'?include_groups=album,single&limit={limit}&offset={offset}'
                    )
                    items = result.get('items', [])
                    if not items:
                        break
                    albums.extend(items)
                    if result.get('next') is None:
                        break
                    offset += limit
                    time.sleep(API_DELAY)
                except Exception as e:
                    # Some Spotify artist profiles have a non-standard limit cap
                    # (e.g. limit=20 returns 400 "Invalid limit" but limit=10 works).
                    # Halve the limit and retry rather than giving up immediately.
                    if limit > 1 and getattr(e, 'http_status', None) == 400:
                        limit //= 2
                        logger.debug(f'Albums fetch for {artist_id}: limit capped, retrying with limit={limit}')
                        continue
                    logger.warning(f'Failed to fetch albums for {artist_id}: {e}')
                    break
        finally:
            _spotipy_logger.setLevel(_orig_level)

        # Sort newest-first, deduplicate by normalised name (avoid remaster dupes)
        def sort_key(a: dict) -> str:
            return pad_release_date(a.get('release_date', ''))

        albums.sort(key=sort_key, reverse=True)

        # Dedup by normalised name (strips "(Deluxe)", "(Live)", etc.). On a
        # collision the preferred release wins even if older: a non-variant over
        # a live/acoustic re-release, then an album over a same-named single
        # (artists routinely release the title track as a single first).
        # Otherwise newest wins, since the list is already newest-first.
        def _is_variant_album(a: dict) -> bool:
            return bool(_VARIANT_ALBUM_RE.search(a.get('name', '')))

        def _preference(a: dict) -> tuple[bool, int]:
            return (not _is_variant_album(a), album_type_rank(a.get('album_type', '')))

        chosen: dict[str, dict] = {}
        for a in albums:
            norm = a.get('name', '').lower().split('(')[0].strip()
            existing = chosen.get(norm)
            if existing is None or _preference(a) > _preference(existing):
                chosen[norm] = a

        return sorted(chosen.values(), key=sort_key, reverse=True)[:MAX_ALBUMS_PER_ARTIST]

    def _get_album_tracks(self, album_id: str) -> list[Track]:
        tracks: list[dict] = []
        offset = 0
        while True:
            try:
                result = self.sp.album_tracks(album_id, limit=50, offset=offset)
                items = result.get('items', [])
                tracks.extend(items)
                if result.get('next') is None:
                    break
                offset += 50
                time.sleep(API_DELAY)
            except Exception as e:
                logger.warning(f'Failed to fetch tracks for album {album_id}: {e}')
                break
        return tracks

