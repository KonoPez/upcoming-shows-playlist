"""
Last.fm integration — score tracks by global listener popularity.

Requires a free API key from https://www.last.fm/api/account/create
Set LASTFM_API_KEY in .env to enable.

The returned score dict maps normalized track names to a log-normalized
popularity score between 0.0 and 1.0. The most-played track for an artist
scores 1.0; other tracks are scored relative to that maximum using log
scaling to handle the power-law distribution of streaming counts.
"""

import logging
import math

import requests

from cache import Cache

logger = logging.getLogger(__name__)

BASE_URL = 'http://ws.audioscrobbler.com/2.0/'
LASTFM_TTL = 7 * 24 * 3600   # 7 days — play counts shift slowly
MAX_TRACKS = 50


def _normalize_title(name: str) -> str:
    """Lowercase + strip for fuzzy matching against Spotify track names."""
    return name.lower().strip()


class LastFmClient:
    def __init__(self, api_key: str, cache: Cache):
        self.api_key = api_key
        self.cache = cache

    def get_popularity_scores(self, artist_name: str) -> dict[str, float]:
        """
        Return {normalized_track_name: popularity_score} for an artist.

        popularity_score = log(playcount) / log(max_playcount), so the
        most-played track scores 1.0 and others scale relative to it.
        Results are cached for LASTFM_TTL seconds.
        """
        cache_key = f'lastfm:{artist_name.lower().strip()}'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        scores = self._fetch_popularity_scores(artist_name)
        self.cache.set(cache_key, scores, LASTFM_TTL)
        return scores

    def _fetch_popularity_scores(self, artist_name: str) -> dict[str, float]:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    'method': 'artist.getTopTracks',
                    'artist': artist_name,
                    'api_key': self.api_key,
                    'format': 'json',
                    'limit': MAX_TRACKS,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'Last.fm API error for "{artist_name}": {e}')
            return {}

        tracks = resp.json().get('toptracks', {}).get('track', [])
        if not tracks:
            logger.debug(f'Last.fm: no tracks found for "{artist_name}"')
            return {}

        counts: list[tuple[str, int]] = []
        for t in tracks:
            name = _normalize_title(t.get('name', ''))
            try:
                count = int(t.get('playcount', 0))
            except (ValueError, TypeError):
                count = 0
            if name and count > 0:
                counts.append((name, count))

        if not counts:
            return {}

        max_count = max(c for _, c in counts)
        log_max = math.log(max_count)

        scores = {name: math.log(count) / log_max for name, count in counts}
        logger.debug(f'Last.fm: "{artist_name}": {len(scores)} tracks scored')
        return scores
