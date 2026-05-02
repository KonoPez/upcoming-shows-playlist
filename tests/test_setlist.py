"""
Tests for setlist.fm integration and setlist score multiplier in scoring.
No real network calls — all HTTP is mocked via unittest.mock.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from sources.models import Track
from sources.setlist import SetlistClient, _normalize_title, _parse_setlist_date, MAX_SHOWS
from playlist_logic.scoring import SETLIST_W, score_track, select_tracks_for_artist


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_setlist(songs: list[str], days_ago: int = 10) -> dict:
    """Build a minimal setlist.fm setlist payload."""
    d = date.today() - timedelta(days=days_ago)
    return {
        'eventDate': d.strftime('%d-%m-%Y'),
        'sets': {'set': [{'song': [{'name': s} for s in songs]}]},
    }


def _make_cache(stored=None) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = stored
    return cache


def _make_track(name: str, album_id: str = 'a1') -> Track:
    return Track(
        id=name.lower().replace(' ', '_'),
        name=name,
        album_id=album_id,
        album_name='Album',
        release_date='2020-01-01',
        release_date_precision='day',
        duration_ms=200_000,
    )


# ── _normalize_title ──────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_lowercases(self):
        assert _normalize_title('From The Start') == 'from the start'

    def test_strips_whitespace(self):
        assert _normalize_title('  hello  ') == 'hello'


# ── _parse_setlist_date ───────────────────────────────────────────────────────

class TestParseSetlistDate:
    def test_valid_date(self):
        assert _parse_setlist_date('15-06-2023') == date(2023, 6, 15)

    def test_invalid_returns_none(self):
        assert _parse_setlist_date('not-a-date') is None

    def test_empty_returns_none(self):
        assert _parse_setlist_date('') is None


# ── SetlistClient ─────────────────────────────────────────────────────────────

class TestSetlistClient:
    def _client(self, cache=None):
        return SetlistClient('fake-key', cache or _make_cache())

    def test_returns_cached_result_without_api_call(self):
        cached = {'song one': 0.8}
        client = self._client(_make_cache(stored=cached))
        with patch('sources.setlist.requests.get') as mock_get:
            result = client.get_setlist_scores('Artist')
        mock_get.assert_not_called()
        assert result == cached

    def test_frequency_is_appearances_over_shows(self):
        setlists = [
            _make_setlist(['Song A', 'Song B'], days_ago=10),
            _make_setlist(['Song A'], days_ago=20),
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': setlists}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        assert abs(result['song a'] - 1.0) < 1e-9   # 2/2
        assert abs(result['song b'] - 0.5) < 1e-9   # 1/2

    def test_caps_at_max_shows(self):
        setlists = [_make_setlist(['Song'], days_ago=i * 3) for i in range(MAX_SHOWS + 5)]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': setlists}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        # frequency = MAX_SHOWS / MAX_SHOWS = 1.0, not inflated by extra shows
        assert abs(result['song'] - 1.0) < 1e-9

    def test_excludes_shows_older_than_one_year(self):
        old_setlist = _make_setlist(['Old Song'], days_ago=400)
        recent_setlist = _make_setlist(['New Song'], days_ago=10)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': [old_setlist, recent_setlist]}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        assert 'old song' not in result
        assert 'new song' in result

    def test_song_counted_once_per_show_even_if_played_twice(self):
        # Encores or duplicates in the data shouldn't inflate frequency above 1.0
        setlist = _make_setlist(['Song A', 'Song A'], days_ago=5)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': [setlist]}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        assert abs(result['song a'] - 1.0) < 1e-9

    def test_empty_setlists_returns_empty_dict(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': []}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        assert result == {}

    def test_setlists_with_no_songs_skipped(self):
        empty_show = {'eventDate': date.today().strftime('%d-%m-%Y'), 'sets': {'set': []}}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': [empty_show]}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            result = self._client().get_setlist_scores('Artist')

        assert result == {}

    def test_api_error_returns_empty_dict(self):
        import requests as req
        with patch('sources.setlist.requests.get', side_effect=req.RequestException('timeout')):
            result = self._client().get_setlist_scores('Artist')
        assert result == {}

    def test_result_is_cached_after_fetch(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'setlist': [_make_setlist(['Song'], days_ago=5)]}
        mock_resp.raise_for_status.return_value = None
        cache = _make_cache()

        with patch('sources.setlist.requests.get', return_value=mock_resp):
            self._client(cache).get_setlist_scores('Artist')

        cache.set.assert_called_once()


# ── Setlist frequency as popularity in score_track ────────────────────────────

class TestScoreTrackSetlistAsPopularity:
    TODAY = date(2024, 6, 1)

    def _score(self, name: str, setlist_scores) -> float:
        track = _make_track(name)
        return score_track(track, {}, {}, self.TODAY, setlist_scores)

    def test_no_setlist_scores_uses_zero_popularity(self):
        # None and empty dict both yield popularity=0.0
        base = self._score('Song', None)
        with_empty = self._score('Song', {})
        assert base == with_empty

    def test_track_in_every_show_gets_full_setlist_weight(self):
        # Test track release_date='2020-01-01' is outside the recency window at
        # TODAY=2024-06-01, so recency=0 for both paths and the difference is
        # purely SETLIST_W × freq.
        no_setlist = self._score('Song', None)
        full_setlist = self._score('Song', {'song': 1.0})
        assert abs(full_setlist - no_setlist - SETLIST_W * 1.0) < 1e-9

    def test_partial_frequency_gives_partial_setlist_weight(self):
        no_setlist = self._score('Song', None)
        half_setlist = self._score('Song', {'song': 0.5})
        assert abs(half_setlist - no_setlist - SETLIST_W * 0.5) < 1e-9

    def test_higher_frequency_scores_higher(self):
        lo = self._score('Song', {'song': 0.25})
        hi = self._score('Song', {'song': 0.75})
        assert hi > lo

    def test_track_not_in_setlist_unaffected(self):
        base = self._score('Song', None)
        with_setlist = self._score('Song', {'other song': 1.0})
        assert base == with_setlist

    def test_matching_is_case_insensitive(self):
        base = self._score('From The Start', None)
        boosted = self._score('From The Start', {'from the start': 1.0})
        assert boosted > base


# ── Setlist scores flow through select_tracks_for_artist ─────────────────────

class TestSelectTracksSetlistIntegration:
    TODAY = date(2024, 6, 1)
    BUDGET = 600_000   # 10 min

    def test_setlist_track_ranked_above_non_setlist(self):
        # Two otherwise identical tracks; setlist presence should lift one above.
        t1 = _make_track('Setlist Hit', album_id='a1')
        t2 = _make_track('Deep Cut', album_id='a2')

        chosen = select_tracks_for_artist(
            tracks=[t1, t2],
            duration_budget_ms=self.BUDGET,
            spotify_familiarity={},
            play_counts={},
            today=self.TODAY,
            setlist_scores={'setlist hit': 1.0},
        )
        # Both fit in budget, but setlist hit should appear first after interleaving
        names = [t.name for t in chosen]
        assert 'Setlist Hit' in names
        assert 'Deep Cut' in names
        assert names.index('Setlist Hit') < names.index('Deep Cut')
