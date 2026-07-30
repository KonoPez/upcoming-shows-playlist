"""
Tests for sources/lastfm.py — Last.fm popularity scoring.
No real network calls — all HTTP is mocked via unittest.mock.
"""

import math
from unittest.mock import MagicMock, patch

import pytest

from sources.lastfm import LastFmClient, MAX_TRACKS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cache(stored=None) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = stored
    return cache


def _make_tracks(names_and_counts: list[tuple[str, int]]) -> list[dict]:
    return [{'name': name, 'playcount': str(count)} for name, count in names_and_counts]


def _client(cache=None) -> LastFmClient:
    return LastFmClient('fake-key', cache or _make_cache())


# ── LastFmClient ──────────────────────────────────────────────────────────────

class TestLastFmClient:
    def test_returns_cached_result_without_api_call(self):
        cached = {'song one': 0.9}
        client = _client(_make_cache(stored=cached))
        with patch('sources.lastfm.requests.get') as mock_get:
            result = client.get_popularity_scores('Artist')
        mock_get.assert_not_called()
        assert result == cached

    def test_most_played_track_scores_one(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Top Song', 1_000_000), ('Other Song', 100)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert abs(result['top song'] - 1.0) < 1e-9

    def test_log_normalisation(self):
        # Two tracks: playcount 1000 and 10. log(10)/log(1000) = 1/3.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Big Hit', 1000), ('Deep Cut', 10)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        expected = math.log(10) / math.log(1000)
        assert abs(result['deep cut'] - expected) < 1e-9

    def test_zero_playcount_tracks_excluded(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Real Song', 5000), ('Ghost Track', 0)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert 'ghost track' not in result
        assert 'real song' in result

    def test_all_zero_playcounts_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Song', 0)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert result == {}

    def test_max_playcount_of_one_returns_empty(self):
        # Regression: when the top track has a single play, log(max)==0 would
        # divide by zero. There's no popularity distribution to rank on, so the
        # function must return {} (no signal) rather than crash.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Song A', 1), ('Song B', 1)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Obscure Artist')

        assert result == {}

    def test_empty_track_list_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'toptracks': {'track': []}}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert result == {}

    def test_api_error_returns_empty_dict(self):
        import requests as req
        with patch('sources.lastfm.requests.get', side_effect=req.RequestException('timeout')):
            result = _client().get_popularity_scores('Artist')
        assert result == {}

    def test_result_is_cached_after_fetch(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Song', 5000)])}
        }
        mock_resp.raise_for_status.return_value = None
        cache = _make_cache()

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            _client(cache).get_popularity_scores('Artist')

        cache.set.assert_called_once()

    def test_scores_are_between_zero_and_one(self):
        tracks = _make_tracks([(f'Song {i}', 10 ** i) for i in range(1, 6)])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'toptracks': {'track': tracks}}
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert all(0.0 <= score <= 1.0 for score in result.values())

    def test_higher_playcount_scores_higher(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('Big', 100_000), ('Small', 1_000)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert result['big'] > result['small']

    def test_name_normalisation(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'toptracks': {'track': _make_tracks([('From The Start', 50_000)])}
        }
        mock_resp.raise_for_status.return_value = None

        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = _client().get_popularity_scores('Artist')

        assert 'from the start' in result
        assert 'From The Start' not in result
