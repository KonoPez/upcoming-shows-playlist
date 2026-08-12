"""
Setlist.fm integration — score tracks by how often they appear in recent setlists.

Requires a free API key from https://www.setlist.fm/settings/apps
Set SETLIST_FM_API_KEY in .env to enable.

The returned score dict maps normalized track names to a frequency between
0.0 and 1.0 (appearances / shows_analyzed). This is used as a multiplier
in scoring.py rather than a standalone signal, so tracks the user hasn't
heard can still surface if they're live staples.
"""

import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests

from cache import Cache

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.setlist.fm/rest/1.0'
SETLIST_TTL = 7 * 24 * 3600   # 7 days — lineups are stable week-to-week
MAX_SHOWS = 10                  # shows to sample per artist
MAX_SPREAD_DAYS = 365           # ignore shows this much older than the artist's latest show


def _parse_setlist_date(date_str: str) -> Optional[date]:
    """Parse setlist.fm date format: dd-MM-yyyy."""
    try:
        day, month, year = date_str.split('-')
        return date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


class SetlistClient:
    def __init__(self, api_key: str, cache: Cache):
        self.cache = cache
        self._headers = {
            'x-api-key': api_key,
            'Accept': 'application/json',
        }

    def get_setlist_scores(self, artist_name: str) -> dict[str, float]:
        """
        Return {normalized_track_name: frequency_score} for an artist.

        frequency_score = appearances across sampled shows / shows_analyzed.
        Covers up to MAX_SHOWS shows within MAX_SPREAD_DAYS of the artist's
        most recent show. Results are cached for SETLIST_TTL seconds.
        """
        cache_key = f'setlist:{artist_name.lower().strip()}'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        scores = self._fetch_setlist_scores(artist_name)
        # Cache even an empty result so a miss doesn't re-hit the API every run.
        self.cache.set(cache_key, scores, SETLIST_TTL)
        return scores

    def _fetch_setlist_scores(self, artist_name: str) -> dict[str, float]:
        time.sleep(1.0)
        try:
            resp = requests.get(
                f'{BASE_URL}/search/setlists',
                headers=self._headers,
                params={'artistName': artist_name, 'p': 1},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f'Setlist.fm API error for "{artist_name}": {e}')
            return {}

        setlists = resp.json().get('setlist', [])
        if not setlists:
            logger.debug(f'Setlist.fm: no setlists found for "{artist_name}"')
            return {}

        # Collect every usable show first — the age window is measured from the
        # artist's own latest show, not from today, so it can't be applied until
        # that show is known.
        shows: list[tuple[date, list[str]]] = []
        for sl in setlists:
            event_date = _parse_setlist_date(sl.get('eventDate', ''))
            if not event_date:
                continue

            songs = [
                song['name'].lower().strip()
                for s in sl.get('sets', {}).get('set', [])
                for song in s.get('song', [])
                if song.get('name') and song['name'].strip()
            ]
            if not songs:
                continue   # skip shows with no setlist data entered yet

            shows.append((event_date, songs))

        if not shows:
            return {}

        # An artist who last toured two years ago still tells us more than no
        # data at all, so there is no absolute age floor. But once they play
        # again, that one fresh show supersedes the whole older run: the anchor
        # moves forward and the stale shows drop out of the window.
        shows.sort(key=lambda s: s[0], reverse=True)
        cutoff = shows[0][0] - timedelta(days=MAX_SPREAD_DAYS)

        counts: dict[str, int] = {}
        shows_analyzed = 0
        for event_date, songs in shows:
            if shows_analyzed >= MAX_SHOWS or event_date < cutoff:
                break

            shows_analyzed += 1
            for key in songs:
                counts[key] = counts.get(key, 0) + 1

        scores = {title: count / shows_analyzed for title, count in counts.items()}
        logger.debug(
            f'Setlist.fm: "{artist_name}": {shows_analyzed} shows, '
            f'{len(scores)} unique tracks found'
        )
        return scores
