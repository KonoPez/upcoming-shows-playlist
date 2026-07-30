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
from typing import Optional

import requests

from cache import Cache

logger = logging.getLogger(__name__)

BASE_URL = 'http://ws.audioscrobbler.com/2.0/'
LASTFM_TTL = 7 * 24 * 3600   # 7 days — play counts shift slowly
MAX_TRACKS = 50
MAX_SIMILAR = 50


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

    def get_artist_listeners(self, artist_name: str) -> Optional[int]:
        """
        Return the total listener count for an artist from Last.fm, or None on failure.
        Used as a global popularity signal for discovery scoring; cached for LASTFM_TTL.
        """
        cache_key = f'lastfm_listeners:{artist_name.lower().strip()}'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached if cached != -1 else None

        count = self._fetch_artist_listeners(artist_name)
        # Cache -1 as a sentinel for "no data" so we don't re-hit the API
        self.cache.set(cache_key, count if count is not None else -1, LASTFM_TTL)
        return count

    def get_similar_artists(self, artist_name: str) -> dict[str, float]:
        """
        Return {similar_artist_name: similarity_weight} for an artist.

        Weights are Last.fm's 0–1 similarity scores.  Returns {} when Last.fm
        has no data or on network error.  Results cached for LASTFM_TTL.
        """
        cache_key = f'lastfm_similar:{artist_name.lower().strip()}'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._fetch_similar_artists(artist_name)
        self.cache.set(cache_key, result, LASTFM_TTL)
        return result

    def _fetch_similar_artists(self, artist_name: str) -> dict[str, float]:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    'method': 'artist.getSimilar',
                    'artist': artist_name,
                    'api_key': self.api_key,
                    'format': 'json',
                    'limit': MAX_SIMILAR,
                    'autocorrect': 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'Last.fm artist.getSimilar error for "{artist_name}": {e}')
            return {}

        similar = resp.json().get('similarartists', {}).get('artist', [])
        result: dict[str, float] = {}
        for entry in similar:
            name = entry.get('name', '').strip()
            try:
                weight = float(entry.get('match', 0))
            except (ValueError, TypeError):
                weight = 0.0
            if name and weight > 0:
                result[name.lower()] = weight
        return result

    def _fetch_artist_listeners(self, artist_name: str) -> Optional[int]:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    'method': 'artist.getInfo',
                    'artist': artist_name,
                    'api_key': self.api_key,
                    'format': 'json',
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'Last.fm artist.getInfo error for "{artist_name}": {e}')
            return None

        artist_info = resp.json().get('artist', {})
        try:
            listeners = int(artist_info.get('stats', {}).get('listeners', 0))
            return listeners if listeners > 0 else None
        except (ValueError, TypeError):
            return None

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
            name = t.get('name', '').lower().strip()
            try:
                count = int(t.get('playcount', 0))
            except (ValueError, TypeError):
                count = 0
            if name and count > 0:
                counts.append((name, count))

        if not counts:
            return {}

        max_count = max(c for _, c in counts)
        if max_count <= 1:
            # Every track has a single play — no popularity distribution to rank
            # on (and log(1) == 0 would divide by zero). Treat as no signal so the
            # Last.fm weight drops out of scoring cleanly, like the no-data case.
            return {}
        log_max = math.log(max_count)

        scores = {name: math.log(count) / log_max for name, count in counts}
        logger.debug(f'Last.fm: "{artist_name}": {len(scores)} tracks scored')
        return scores
