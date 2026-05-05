"""
High-level Spotify operations: playlist management, user history, discography fetching.
"""

import logging
import re
import time
from typing import Optional

import spotipy

from cache import Cache
from sources.models import Track

logger = logging.getLogger(__name__)

ARTIST_TRACKS_TTL = 30 * 24 * 3600   # 30 days — discographies don't change fast
FAMILIARITY_TTL = 6 * 3600            # 6 hours — top tracks don't shift meaningfully intra-day
RECENTLY_PLAYED_TTL = 5 * 60          # 5 min — deduplicates the two callers within a single run
API_DELAY = 0.1                        # 100 ms between calls
MAX_ALBUMS_PER_ARTIST = 15            # most recent studio albums + singles


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

    def get_artist_top_scores(self, cache: Cache) -> dict[str, float]:
        """
        Build a {artist_id: score} map from Spotify's top-artists API.
        Mirrors get_user_familiarity but at the artist level.

        Scores:
          short_term top artists (≈4 weeks)   → 1.0
          medium_term top artists (≈6 months) → 0.8
          long_term top artists (years)        → 0.6
        Artists in multiple lists get the highest score.
        Cached for FAMILIARITY_TTL (6 h).
        """
        cache_key = 'artist_top_scores'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        scores: dict[str, float] = {}
        for time_range, score in [
            ('short_term', 1.0),
            ('medium_term', 0.8),
            ('long_term', 0.6),
        ]:
            try:
                result = self.sp.current_user_top_artists(limit=50, time_range=time_range)
                for artist in result.get('items', []):
                    aid = artist.get('id')
                    if aid:
                        scores[aid] = max(scores.get(aid, 0.0), score)
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

    # ── Artist discography ────────────────────────────────────────────────────

    def get_artist_tracks(self, artist_id: str, cache: Cache) -> list[Track]:
        """Return tracks for an artist's discography. Cached for ARTIST_TRACKS_TTL."""
        cache_key = f'artist_tracks:{artist_id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return [Track.from_dict(t) for t in cached]

        tracks = self._fetch_artist_tracks(artist_id)
        if tracks:
            cache.set(cache_key, [t.to_dict() for t in tracks], ARTIST_TRACKS_TTL)
        return tracks

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

    def _is_variant_recording(self, track_name: str, album_name: str) -> bool:
        """Return True if the track is a live, acoustic, remix, instrumental, or demo version."""
        return bool(
            self._VARIANT_ALBUM_RE.search(album_name)
            or self._VARIANT_TRACK_RE.search(track_name)
        )

    def _fetch_artist_tracks(self, artist_id: str) -> list[Track]:
        albums = self._get_artist_albums(artist_id)
        if not albums:
            return []

        # Collect tracks, deduplicating by normalised name (keeps newest version)
        by_name: dict[str, Track] = {}

        for album in albums:
            album_id = album['id']
            album_name = album.get('name', '')
            release_date = album.get('release_date', '')
            release_date_precision = album.get('release_date_precision', 'year')

            for track in self._get_album_tracks(album_id):
                tid = track.get('id')
                if not tid:
                    continue

                track_name = track.get('name', '')
                if self._is_variant_recording(track_name, album_name):
                    logger.debug(f'Skipping variant recording: "{track_name}" ({album_name})')
                    continue

                name_key = track_name.lower().strip()
                existing = by_name.get(name_key)

                # Keep the version from the most recently released album
                if existing and existing.release_date >= release_date:
                    continue

                by_name[name_key] = Track(
                    id=tid,
                    name=track_name,
                    duration_ms=track.get('duration_ms', 0),
                    album_id=album_id,
                    album_name=album_name,
                    release_date=release_date,
                    release_date_precision=release_date_precision,
                )

        if not by_name:
            return []

        all_tracks = list(by_name.values())
        logger.debug(
            f'Artist {artist_id}: {len(all_tracks)} tracks from {len(albums)} albums'
        )
        return all_tracks

    def _get_artist_albums(self, artist_id: str) -> list[Track]:
        """Return the most recent MAX_ALBUMS_PER_ARTIST albums + singles."""
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
            rd = a.get('release_date', '0000')
            if len(rd) == 4:
                rd += '-01-01'
            elif len(rd) == 7:
                rd += '-01'
            return rd

        albums.sort(key=sort_key, reverse=True)

        seen_names: set[str] = set()
        deduped: list[dict] = []
        for a in albums:
            # Normalise album name: strip "(Deluxe)", "(Remaster)", etc.
            norm = (
                a.get('name', '')
                .lower()
                .split('(')[0]
                .strip()
            )
            if norm not in seen_names:
                seen_names.add(norm)
                deduped.append(a)
            if len(deduped) >= MAX_ALBUMS_PER_ARTIST:
                break

        return deduped

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

