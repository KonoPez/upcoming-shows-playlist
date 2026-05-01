"""
High-level Spotify operations: playlist management, user history, discography fetching.
"""

import logging
import re
import time
from typing import Optional

import spotipy

from cache import Cache

logger = logging.getLogger(__name__)

ARTIST_TRACKS_TTL = 30 * 24 * 3600   # 30 days — discographies don't change fast
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
        self, name: str, playlist_id: Optional[str] = None
    ) -> str:
        """
        Return the ID of the managed playlist.
        Uses the existing playlist_id if still valid; otherwise creates a new one.
        """
        if playlist_id:
            try:
                pl = self.sp.playlist(playlist_id, fields='id,name')
                if pl and pl.get('id'):
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
            'description': 'Auto-updated: tracks to prep for upcoming concerts',
        })
        logger.info(f'Created playlist: "{name}" ({pl["id"]})')
        return pl['id']

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

    def get_user_familiarity(self) -> dict[str, float]:
        """
        Build a {track_id: familiarity_score} map from Spotify's API signals.

        Scores:
          short_term top tracks (≈4 weeks)   → 1.0
          medium_term top tracks (≈6 months) → 0.8
          long_term top tracks (years)        → 0.6
          recently played                     → 0.5
        Tracks in multiple lists get the highest score.
        """
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

        try:
            result = self.sp.current_user_recently_played(limit=50)
            for item in result.get('items', []):
                tid = item.get('track', {}).get('id')
                if tid:
                    familiarity[tid] = max(familiarity.get(tid, 0.0), 0.5)
        except Exception as e:
            logger.warning(f'Failed to fetch recently played: {e}')

        return familiarity

    def get_recently_played_with_artists(self) -> list[dict]:
        """
        Return play events for local history accumulation.
        Each entry: {track_id, artist_id, played_at}.
        """
        plays: list[dict] = []
        try:
            result = self.sp.current_user_recently_played(limit=50)
            for item in result.get('items', []):
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
        except Exception as e:
            logger.warning(f'Failed to fetch recently played for history: {e}')
        return plays

    # ── Artist discography ────────────────────────────────────────────────────

    def get_artist_tracks(self, artist_id: str, cache: Cache) -> list[dict]:
        """
        Return a list of track dicts for an artist's discography.
        Cached for ARTIST_TRACKS_TTL.

        Each track dict: {id, name, popularity, album_id, album_name,
                          release_date, release_date_precision, duration_ms}
        """
        cache_key = f'artist_tracks:{artist_id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        tracks = self._fetch_artist_tracks(artist_id)
        if tracks:
            cache.set(cache_key, tracks, ARTIST_TRACKS_TTL)
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

    def _fetch_artist_tracks(self, artist_id: str) -> list[dict]:
        albums = self._get_artist_albums(artist_id)
        if not albums:
            return []

        # Collect tracks, deduplicating by normalised name (keeps newest version)
        by_name: dict[str, dict] = {}

        for album in albums:
            album_meta = {
                'album_id': album['id'],
                'album_name': album.get('name', ''),
                'release_date': album.get('release_date', ''),
                'release_date_precision': album.get('release_date_precision', 'year'),
            }
            release_date = album_meta['release_date']

            for track in self._get_album_tracks(album['id']):
                tid = track.get('id')
                if not tid:
                    continue

                track_name = track.get('name', '')
                if self._is_variant_recording(track_name, album_meta['album_name']):
                    logger.debug(f'Skipping variant recording: "{track_name}" ({album_meta["album_name"]})')
                    continue

                name_key = track_name.lower().strip()
                existing = by_name.get(name_key)

                # Keep the version from the most recently released album
                if existing and existing['release_date'] >= release_date:
                    continue

                by_name[name_key] = {
                    'id': tid,
                    'name': track_name,
                    'duration_ms': track.get('duration_ms', 0),
                    'popularity': 0,   # filled by batch call below
                    **album_meta,
                }

        if not by_name:
            return []

        # Batch-fetch popularity scores
        all_tracks = list(by_name.values())
        track_ids = [t['id'] for t in all_tracks]
        popularity = self._get_popularity_batch(track_ids)
        for t in all_tracks:
            t['popularity'] = popularity.get(t['id'], 0)

        logger.debug(
            f'Artist {artist_id}: {len(all_tracks)} tracks from {len(albums)} albums'
        )
        return all_tracks

    def _get_artist_albums(self, artist_id: str) -> list[dict]:
        """Return the most recent MAX_ALBUMS_PER_ARTIST albums + singles."""
        import logging as _logging
        _spotipy_logger = _logging.getLogger('spotipy.client')

        albums: list[dict] = []
        offset = 0
        limit = 20
        _suppress_spotipy = False

        while len(albums) < MAX_ALBUMS_PER_ARTIST * 2:  # over-fetch, then trim
            if _suppress_spotipy:
                _spotipy_logger.setLevel(_logging.CRITICAL)
            try:
                # Embed params in the URL string rather than passing as kwargs.
                # When kwargs are passed, requests.urlencode encodes the comma in
                # "album,single" as %2C, which Spotify rejects with a 400 error.
                # An inline query string is left unmodified by requests.
                result = self.sp._get(
                    f'artists/{artist_id}/albums'
                    f'?include_groups=album,single&limit={limit}&offset={offset}'
                )
                _spotipy_logger.setLevel(_logging.NOTSET)
                _suppress_spotipy = False
                items = result.get('items', [])
                if not items:
                    break
                albums.extend(items)
                if result.get('next') is None:
                    break
                offset += limit
                time.sleep(API_DELAY)
            except Exception as e:
                _spotipy_logger.setLevel(_logging.NOTSET)
                # Some Spotify artist profiles have a non-standard limit cap
                # (e.g. limit=20 returns 400 "Invalid limit" but limit=10 works).
                # Halve the limit and retry rather than giving up immediately.
                if limit > 1 and getattr(e, 'http_status', None) == 400:
                    limit //= 2
                    _suppress_spotipy = True
                    logger.debug(f'Albums fetch for {artist_id}: limit capped, retrying with limit={limit}')
                    continue
                logger.warning(f'Failed to fetch albums for {artist_id}: {e}')
                break

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

    def _get_album_tracks(self, album_id: str) -> list[dict]:
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

    def _get_popularity_batch(self, track_ids: list[str]) -> dict[str, int]:
        """Batch-fetch popularity for up to 50 track IDs at a time.

        Uses _get() directly to avoid spotipy's tracks() wrapper passing
        market=None as a query parameter, which Spotify rejects with 403.
        """
        result: dict[str, int] = {}
        for i in range(0, len(track_ids), 50):
            batch = track_ids[i:i + 50]
            try:
                data = self.sp._get(f'tracks?ids={",".join(batch)}')
                for t in data.get('tracks', []):
                    if t and t.get('id'):
                        result[t['id']] = t.get('popularity', 0)
                time.sleep(API_DELAY)
            except Exception as e:
                logger.warning(f'Failed to fetch track details: {e}')
        return result
