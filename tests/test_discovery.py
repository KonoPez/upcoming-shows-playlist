"""
Tests for the discovery playlist pipeline.

Covers:
  - playlist_logic/discovery_weighting.py  — pure scoring and allocation functions
  - cache.get_artist_play_counts           — DB helper used by discovery
  - sources/ticketmaster._concerts_from_event / get_local_events — new TM methods
  - sources/lastfm.get_artist_listeners    — new Last.fm method
  - Regression: every artist selected by select_discovery_artists receives a
    non-zero duration budget from allocate_slots (min_duration_ms=0 fix)
"""

import math
from datetime import date, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from cache import Cache
from playlist_logic.discovery_weighting import (
    ENJOYMENT_EXPONENT,
    FAMILIARITY_W,
    POPULARITY_W,
    PROXIMITY_EXPONENT,
    compute_artist_familiarity_scores,
    compute_discovery_weights,
    score_artist_enjoyment,
    select_discovery_artists,
)
from playlist_logic.weighting import allocate_slots, concert_weight
from sources.models import Artist, Concert
from sources.ticketmaster import TicketmasterClient, _concert_from_dict, _concerts_from_event


_TODAY = date(2024, 6, 1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _artist(artist_id: str, days_until: int) -> Artist:
    return Artist(
        spotify_id=artist_id,
        name=artist_id,
        concerts=[Concert(
            event_name='Test Show',
            artist_name=artist_id,
            event_date=_TODAY + timedelta(days=days_until),
            venue='Test Venue',
            source='ticketmaster_discovery',
        )],
    )


def _tm_event(
    artist_names: list[str],
    local_date: str = '2024-07-01',
    venue: str = 'The Sylvee',
    spotify_urls: Optional[dict] = None,
) -> dict:
    """Build a minimal TM event dict for _concerts_from_event tests."""
    spotify_urls = spotify_urls or {}
    attractions = []
    for name in artist_names:
        attr: dict = {'name': name}
        if name in spotify_urls:
            attr['externalLinks'] = {'spotify': [{'url': spotify_urls[name]}]}
        attractions.append(attr)
    return {
        'name': f'{artist_names[0]} at {venue}',
        'dates': {'start': {'localDate': local_date}},
        '_embedded': {
            'attractions': attractions,
            'venues': [{'name': venue}],
        },
    }


# ── score_artist_enjoyment ────────────────────────────────────────────────────

class TestScoreArtistEnjoyment:
    def test_no_popularity_returns_familiarity(self):
        assert score_artist_enjoyment(0.8, None) == 0.8

    def test_zero_familiarity_no_popularity_is_zero(self):
        assert score_artist_enjoyment(0.0, None) == 0.0

    def test_both_signals_blended(self):
        fam, pop = 1.0, 0.0
        expected = (FAMILIARITY_W * fam + POPULARITY_W * pop) / (FAMILIARITY_W + POPULARITY_W)
        assert abs(score_artist_enjoyment(fam, pop) - expected) < 1e-9

    def test_full_familiarity_and_popularity_is_one(self):
        assert abs(score_artist_enjoyment(1.0, 1.0) - 1.0) < 1e-9

    def test_higher_familiarity_raises_score(self):
        lo = score_artist_enjoyment(0.2, 0.5)
        hi = score_artist_enjoyment(0.8, 0.5)
        assert hi > lo

    def test_higher_popularity_raises_score(self):
        lo = score_artist_enjoyment(0.5, 0.2)
        hi = score_artist_enjoyment(0.5, 0.8)
        assert hi > lo

    def test_weights_sum_to_one_when_both_available(self):
        # With fam=0 and pop=0, both signals present → score should be 0
        assert score_artist_enjoyment(0.0, 0.0) == 0.0


# ── compute_artist_familiarity_scores ─────────────────────────────────────────

class TestComputeArtistFamiliarityScores:
    def test_all_zeros_when_no_data(self):
        scores = compute_artist_familiarity_scores(['a1', 'a2'], {}, {})
        assert scores == {'a1': 0.0, 'a2': 0.0}

    def test_spotify_top_score_used_directly(self):
        scores = compute_artist_familiarity_scores(['a1'], {'a1': 0.8}, {})
        assert abs(scores['a1'] - 0.8) < 1e-9

    def test_only_artist_with_plays_scores_one(self):
        # a1 has 5 plays; nobody else has any. Within candidates, a1 is the max.
        scores = compute_artist_familiarity_scores(['a1', 'a2'], {}, {'a1': 5})
        assert abs(scores['a1'] - 1.0) < 1e-9
        assert scores['a2'] == 0.0

    def test_normalization_is_relative_to_candidate_max(self):
        # a1 has 4 plays, a2 has 8 plays. max is 8.
        # a1 score = log(5)/log(9), a2 score = log(9)/log(9) = 1.0
        scores = compute_artist_familiarity_scores(['a1', 'a2'], {}, {'a1': 4, 'a2': 8})
        expected_a1 = math.log(5) / math.log(9)
        assert abs(scores['a2'] - 1.0) < 1e-9
        assert abs(scores['a1'] - expected_a1) < 1e-9

    def test_candidate_with_few_plays_still_scores_above_zero(self):
        # Key regression: 3 plays when global max is 500 should still score > 0
        # because normalization is within the candidate set.
        # Here, candidate max is also 3, so score should be 1.0.
        scores = compute_artist_familiarity_scores(
            ['a1'], {}, {'a1': 3, 'non_candidate': 500}
        )
        assert abs(scores['a1'] - 1.0) < 1e-9

    def test_max_of_spotify_and_play_history_taken(self):
        # Spotify says 0.3; play history (5/5 max) says 1.0 → use 1.0
        scores = compute_artist_familiarity_scores(
            ['a1'], {'a1': 0.3}, {'a1': 5}
        )
        assert abs(scores['a1'] - 1.0) < 1e-9

    def test_artists_not_in_top_scores_or_plays_score_zero(self):
        scores = compute_artist_familiarity_scores(['a1', 'a2', 'a3'], {'a2': 0.5}, {})
        assert scores['a1'] == 0.0
        assert scores['a3'] == 0.0

    def test_output_keys_match_candidate_ids(self):
        candidates = ['x', 'y', 'z']
        scores = compute_artist_familiarity_scores(candidates, {}, {})
        assert set(scores.keys()) == set(candidates)


# ── select_discovery_artists ──────────────────────────────────────────────────

class TestSelectDiscoveryArtists:
    def test_returns_tuple_of_ids_and_scores(self):
        ids, scores = select_discovery_artists(['a1'], {'a1': 0.8}, {}, 10, 0.1)
        assert isinstance(ids, list)
        assert isinstance(scores, dict)

    def test_below_floor_excluded(self):
        fam = {'a1': 0.5, 'a2': 0.05}   # a2 below 0.1 floor
        ids, _ = select_discovery_artists(['a1', 'a2'], fam, {}, 10, 0.10)
        assert 'a1' in ids
        assert 'a2' not in ids

    def test_cap_respected(self):
        fam = {f'a{i}': 0.9 - i * 0.05 for i in range(15)}
        ids, _ = select_discovery_artists(list(fam.keys()), fam, {}, 10, 0.0)
        assert len(ids) == 10

    def test_ordered_by_score_descending(self):
        fam = {'a1': 0.3, 'a2': 0.9, 'a3': 0.6}
        ids, _ = select_discovery_artists(['a1', 'a2', 'a3'], fam, {}, 10, 0.0)
        assert ids == ['a2', 'a3', 'a1']

    def test_scores_dict_matches_selected_ids(self):
        fam = {'a1': 0.8, 'a2': 0.5}
        ids, scores = select_discovery_artists(['a1', 'a2'], fam, {}, 10, 0.0)
        assert set(scores.keys()) == set(ids)

    def test_all_below_floor_returns_empty(self):
        fam = {'a1': 0.05, 'a2': 0.02}
        ids, scores = select_discovery_artists(['a1', 'a2'], fam, {}, 10, 0.10)
        assert ids == []
        assert scores == {}

    def test_empty_candidates_returns_empty(self):
        ids, scores = select_discovery_artists([], {}, {}, 10, 0.1)
        assert ids == [] and scores == {}

    def test_raw_listeners_used_for_scoring(self):
        # a1: high familiarity, low popularity
        # a2: low familiarity, high popularity (1M listeners vs a1's 1k)
        fam = {'a1': 0.8, 'a2': 0.1}
        listeners = {'a1': 1_000, 'a2': 1_000_000}
        ids, scores = select_discovery_artists(['a1', 'a2'], fam, listeners, 10, 0.0)
        # a1 should still lead due to familiarity weight dominance (0.75)
        assert ids[0] == 'a1'
        # but a2's score is boosted by popularity vs having no listeners
        _, scores_no_pop = select_discovery_artists(['a1', 'a2'], fam, {}, 10, 0.0)
        assert scores['a2'] > scores_no_pop['a2']

    def test_without_lastfm_familiarity_carries_full_weight(self):
        fam = {'a1': 0.6}
        ids, scores = select_discovery_artists(['a1'], fam, {}, 10, 0.0)
        # No popularity data → score_artist_enjoyment returns familiarity directly
        assert abs(scores['a1'] - 0.6) < 1e-9


# ── compute_discovery_weights ─────────────────────────────────────────────────

class TestComputeDiscoveryWeights:
    def test_higher_enjoyment_raises_weight(self):
        artists = {'a1': _artist('a1', 30), 'a2': _artist('a2', 30)}
        scores  = {'a1': 0.9, 'a2': 0.3}
        weights = compute_discovery_weights(artists, scores, _TODAY)
        assert weights['a1'] > weights['a2']

    def test_closer_concert_raises_weight(self):
        artists = {'near': _artist('near', 7), 'far': _artist('far', 60)}
        scores  = {'near': 0.8, 'far': 0.8}
        weights = compute_discovery_weights(artists, scores, _TODAY)
        assert weights['near'] > weights['far']

    def test_zero_enjoyment_produces_zero_weight(self):
        artists = {'a1': _artist('a1', 30)}
        weights = compute_discovery_weights(artists, {'a1': 0.0}, _TODAY)
        assert weights['a1'] == 0.0

    def test_proximity_exponent_applied(self):
        # For two artists with same enjoyment, ratio should reflect proximity^PROXIMITY_EXPONENT
        artists = {'near': _artist('near', 10), 'far': _artist('far', 50)}
        scores  = {'near': 1.0, 'far': 1.0}
        weights = compute_discovery_weights(artists, scores, _TODAY)
        prox_near = concert_weight(10) ** PROXIMITY_EXPONENT
        prox_far  = concert_weight(50) ** PROXIMITY_EXPONENT
        expected_ratio = prox_near / prox_far
        actual_ratio   = weights['near'] / weights['far']
        assert abs(actual_ratio - expected_ratio) < 1e-9

    def test_enjoyment_exponent_applied(self):
        # Same proximity, different enjoyment — ratio follows enjoyment^ENJOYMENT_EXPONENT
        artists = {'strong': _artist('strong', 30), 'weak': _artist('weak', 30)}
        scores  = {'strong': 0.9, 'weak': 0.3}
        weights = compute_discovery_weights(artists, scores, _TODAY)
        expected_ratio = (0.9 ** ENJOYMENT_EXPONENT) / (0.3 ** ENJOYMENT_EXPONENT)
        actual_ratio   = weights['strong'] / weights['weak']
        assert abs(actual_ratio - expected_ratio) < 1e-9

    def test_empty_artists_returns_empty(self):
        assert compute_discovery_weights({}, {}, _TODAY) == {}


# ── Regression: every selected artist gets a slot ────────────────────────────

class TestEverySelectedArtistGetsSlot:
    """
    With min_duration_ms=0, allocate_slots must return a non-zero budget for
    every artist passed to it, even those with very low weights. This ensures
    that the three artists previously excluded from the discovery playlist
    (because their proportional share fell below the old 210_000 ms floor)
    now always receive at least enough budget for select_tracks_for_artist to
    return at least one track.
    """

    def test_all_artists_receive_nonzero_budget(self):
        # 10 artists with weights spanning two orders of magnitude
        weights = {f'a{i}': 1.0 / (i + 1) for i in range(10)}
        slots = allocate_slots(weights, target_duration_ms=7_200_000, min_duration_ms=0)
        assert set(slots.keys()) == set(weights.keys())
        assert all(v > 0 for v in slots.values()), \
            f'Some artists got zero budget: {[(k,v) for k,v in slots.items() if v==0]}'

    def test_total_still_equals_target(self):
        weights = {f'a{i}': 1.0 / (i + 1) for i in range(10)}
        slots = allocate_slots(weights, target_duration_ms=7_200_000, min_duration_ms=0)
        assert sum(slots.values()) == 7_200_000

    def test_tiny_weight_artist_gets_at_least_one_track(self):
        """
        An artist with a very small weight gets a small budget, but
        select_tracks_for_artist rounds up to the first whole track.
        """
        from sources.models import Track
        from playlist_logic.scoring import select_tracks_for_artist

        weights = {'dominant': 999.0, 'tiny': 1.0}
        slots = allocate_slots(weights, target_duration_ms=7_200_000, min_duration_ms=0)

        tiny_budget = slots['tiny']
        assert tiny_budget > 0   # budget is positive

        tracks = [Track(
            id='t1', name='Song', release_date='2020-01-01',
            release_date_precision='day', duration_ms=210_000,
            album_id='alb', album_name='Album',
        )]
        selected = select_tracks_for_artist(tracks, tiny_budget, {}, {}, _TODAY)
        # Even with a tiny budget, greedy selection returns the first track
        assert len(selected) >= 1


# ── cache.get_artist_play_counts ──────────────────────────────────────────────

class TestGetArtistPlayCounts:
    @pytest.fixture
    def cache(self, tmp_path):
        return Cache(db_path=tmp_path / 'test.db')

    def test_empty_when_no_plays(self, cache):
        assert cache.get_artist_play_counts() == {}

    def test_aggregates_across_tracks_per_artist(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a1', 'played_at': '2024-01-02T10:00:00Z'},
            {'track_id': 't3', 'artist_id': 'a1', 'played_at': '2024-01-03T10:00:00Z'},
        ]
        cache.record_plays(plays)
        counts = cache.get_artist_play_counts()
        assert counts['a1'] == 3

    def test_returns_all_artists(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a2', 'played_at': '2024-01-01T11:00:00Z'},
            {'track_id': 't3', 'artist_id': 'a3', 'played_at': '2024-01-01T12:00:00Z'},
        ]
        cache.record_plays(plays)
        counts = cache.get_artist_play_counts()
        assert set(counts.keys()) == {'a1', 'a2', 'a3'}

    def test_counts_each_play_separately(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-02T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a1', 'played_at': '2024-01-03T10:00:00Z'},
        ]
        cache.record_plays(plays)
        counts = cache.get_artist_play_counts()
        assert counts['a1'] == 3   # 2 plays of t1 + 1 of t2

    def test_different_artists_counted_independently(self, cache):
        plays = [
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-01T10:00:00Z'},
            {'track_id': 't1', 'artist_id': 'a1', 'played_at': '2024-01-02T10:00:00Z'},
            {'track_id': 't2', 'artist_id': 'a2', 'played_at': '2024-01-01T11:00:00Z'},
        ]
        cache.record_plays(plays)
        counts = cache.get_artist_play_counts()
        assert counts['a1'] == 2
        assert counts['a2'] == 1


# ── _concerts_from_event ──────────────────────────────────────────────────────

class TestConcertsFromEvent:
    def test_single_artist_event_returns_headliner(self):
        concerts = _concerts_from_event(_tm_event(['Good Kid']))
        assert len(concerts) == 1
        assert concerts[0].artist_name == 'Good Kid'
        assert concerts[0].is_opener is False

    def test_first_attraction_is_headliner(self):
        concerts = _concerts_from_event(_tm_event(['Headliner', 'Opener A', 'Opener B']))
        assert concerts[0].is_opener is False
        assert concerts[0].artist_name == 'Headliner'

    def test_subsequent_attractions_are_openers(self):
        concerts = _concerts_from_event(_tm_event(['Headliner', 'Opener A', 'Opener B']))
        assert concerts[1].is_opener is True
        assert concerts[2].is_opener is True

    def test_source_is_ticketmaster_discovery(self):
        concerts = _concerts_from_event(_tm_event(['Artist']))
        assert concerts[0].source == 'ticketmaster_discovery'

    def test_date_parsed_correctly(self):
        concerts = _concerts_from_event(_tm_event(['Artist'], local_date='2024-09-15'))
        assert concerts[0].event_date == date(2024, 9, 15)

    def test_venue_name_propagated(self):
        concerts = _concerts_from_event(_tm_event(['Artist'], venue='The Orpheum'))
        assert concerts[0].venue == 'The Orpheum'

    def test_missing_date_returns_empty(self):
        event = _tm_event(['Artist'])
        del event['dates']['start']['localDate']
        assert _concerts_from_event(event) == []

    def test_empty_attractions_returns_empty(self):
        event = _tm_event(['Artist'])
        event['_embedded']['attractions'] = []
        assert _concerts_from_event(event) == []

    def test_spotify_id_extracted_from_external_links(self):
        concerts = _concerts_from_event(_tm_event(
            ['Artist'],
            spotify_urls={'Artist': 'https://open.spotify.com/artist/abc123'},
        ))
        assert concerts[0].tm_spotify_id == 'abc123'

    def test_missing_spotify_link_gives_none(self):
        concerts = _concerts_from_event(_tm_event(['Artist']))
        assert concerts[0].tm_spotify_id is None

    def test_correct_concert_count_for_multi_artist_event(self):
        concerts = _concerts_from_event(_tm_event(['A', 'B', 'C']))
        assert len(concerts) == 3

    def test_blank_attraction_name_skipped(self):
        event = _tm_event(['Good Kid'])
        event['_embedded']['attractions'].append({'name': ''})
        concerts = _concerts_from_event(event)
        assert all(c.artist_name != '' for c in concerts)


# ── TicketmasterClient.get_local_events ───────────────────────────────────────

class TestGetLocalEvents:
    def _client(self, tmp_path):
        return TicketmasterClient(api_key='fake', cache=Cache(db_path=tmp_path / 'test.db'))

    def _response(self, artist_names=None, total_pages=1):
        artist_names = artist_names or ['Artist One', 'Artist Two']
        return {
            '_embedded': {
                'events': [_tm_event(artist_names, local_date='2024-07-15')]
            },
            'page': {'size': 1, 'totalElements': 1, 'totalPages': total_pages, 'number': 0},
        }

    def test_returns_concert_objects(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response(['Artist One'])
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp):
            concerts = client.get_local_events(window_days=60, city='Madison, WI')
        assert all(isinstance(c, Concert) for c in concerts)

    def test_passes_city_param_to_api(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response()
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp) as mock_get:
            client.get_local_events(window_days=30, city='Chicago, IL')
        call_kwargs = mock_get.call_args[1]['params']
        assert call_kwargs['city'] == 'Chicago, IL'
        assert 'latlong' not in call_kwargs

    def test_passes_latlong_param_to_api(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response()
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp) as mock_get:
            client.get_local_events(window_days=30, latlong='41.85,-87.65')
        call_kwargs = mock_get.call_args[1]['params']
        assert call_kwargs['latlong'] == '41.85,-87.65'
        assert 'city' not in call_kwargs

    def test_cache_hit_skips_http(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response()
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp):
            client.get_local_events(window_days=60, city='Madison, WI')
        # Second call should be served from cache
        with patch('sources.ticketmaster.requests.get') as mock_get2:
            client.get_local_events(window_days=60, city='Madison, WI')
            mock_get2.assert_not_called()

    def test_api_error_returns_empty(self, tmp_path):
        import requests as req
        client = self._client(tmp_path)
        with patch('sources.ticketmaster.requests.get',
                   side_effect=req.RequestException('timeout')):
            concerts = client.get_local_events(window_days=60, city='Madison, WI')
        assert concerts == []

    def test_empty_events_returns_empty(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            '_embedded': {'events': []},
            'page': {'totalPages': 1},
        }
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp):
            concerts = client.get_local_events(window_days=60, city='Madison, WI')
        assert concerts == []

    def test_raises_when_neither_latlong_nor_city(self, tmp_path):
        client = self._client(tmp_path)
        with pytest.raises(ValueError, match='latlong or city'):
            client.get_local_events(window_days=60)

    def test_radius_passed_to_api(self, tmp_path):
        client = self._client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response()
        with patch('sources.ticketmaster.requests.get', return_value=mock_resp) as mock_get:
            client.get_local_events(window_days=60, city='Madison', radius_miles=75)
        call_kwargs = mock_get.call_args[1]['params']
        assert call_kwargs['radius'] == '75'


# ── LastFmClient.get_artist_listeners ─────────────────────────────────────────

class TestGetArtistListeners:
    def _client(self, stored=None):
        from sources.lastfm import LastFmClient
        cache = MagicMock()
        cache.get.return_value = stored
        return LastFmClient('fake-key', cache)

    def _response(self, listeners: int):
        return {'artist': {'stats': {'listeners': str(listeners), 'playcount': '999'}}}

    def test_returns_listener_count(self):
        from sources.lastfm import LastFmClient
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response(500_000)
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_artist_listeners('Artist')
        assert result == 500_000

    def test_returns_cached_result_without_api_call(self):
        client = self._client(stored=250_000)
        with patch('sources.lastfm.requests.get') as mock_get:
            result = client.get_artist_listeners('Artist')
            mock_get.assert_not_called()
        assert result == 250_000

    def test_cached_sentinel_minus_one_returns_none(self):
        # -1 is stored when a previous fetch returned no data, to avoid re-hitting the API
        client = self._client(stored=-1)
        with patch('sources.lastfm.requests.get') as mock_get:
            result = client.get_artist_listeners('Artist')
            mock_get.assert_not_called()
        assert result is None

    def test_api_error_returns_none(self):
        import requests as req
        client = self._client()
        with patch('sources.lastfm.requests.get',
                   side_effect=req.RequestException('timeout')):
            result = client.get_artist_listeners('Artist')
        assert result is None

    def test_zero_listeners_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response(0)
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_artist_listeners('Artist')
        assert result is None

    def test_result_cached_after_fetch(self):
        from sources.lastfm import LastFmClient
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response(100_000)
        mock_resp.raise_for_status.return_value = None
        cache = MagicMock()
        cache.get.return_value = None
        client = LastFmClient('fake-key', cache)
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            client.get_artist_listeners('Artist')
        cache.set.assert_called_once()
        stored_value = cache.set.call_args[0][1]
        assert stored_value == 100_000

    def test_none_result_cached_as_sentinel(self):
        from sources.lastfm import LastFmClient
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response(0)   # 0 → None
        mock_resp.raise_for_status.return_value = None
        cache = MagicMock()
        cache.get.return_value = None
        client = LastFmClient('fake-key', cache)
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            client.get_artist_listeners('Artist')
        stored_value = cache.set.call_args[0][1]
        assert stored_value == -1   # sentinel stored, not None


# ── resolve_calendar_ids ──────────────────────────────────────────────────────

class TestResolveCalendarIds:
    """Tests for main.resolve_calendar_ids."""

    def _run(self, names: list, resolve_map: dict) -> set:
        """Call resolve_calendar_ids with a stubbed resolve_artist."""
        from main import resolve_calendar_ids
        cache = MagicMock()

        def fake_resolve(name, sp_raw, cache, **kwargs):
            return resolve_map.get(name)

        with patch('main.resolve_artist', side_effect=fake_resolve):
            return resolve_calendar_ids(names, sp_raw=object(), cache=cache)

    def test_empty_names_returns_empty_set(self):
        assert self._run([], {}) == set()

    def test_resolves_names_to_ids(self):
        result = self._run(
            ['Artist A', 'Artist B'],
            {'Artist A': 'id_a', 'Artist B': 'id_b'},
        )
        assert result == {'id_a', 'id_b'}

    def test_unresolvable_name_skipped(self):
        """Names that resolve to None don't contribute an ID."""
        result = self._run(
            ['Known Artist', 'Unknown Act'],
            {'Known Artist': 'id_known', 'Unknown Act': None},
        )
        assert result == {'id_known'}
        assert None not in result

    def test_all_unresolvable_returns_empty(self):
        result = self._run(['A', 'B'], {'A': None, 'B': None})
        assert result == set()

    def test_duplicate_names_produce_one_id(self):
        """The same name appearing twice maps to the same ID once."""
        result = self._run(
            ['Same Artist', 'Same Artist'],
            {'Same Artist': 'id_same'},
        )
        assert result == {'id_same'}

    def test_resolve_artist_called_for_each_name(self):
        """resolve_artist is invoked once per name in the input list."""
        from main import resolve_calendar_ids
        cache = MagicMock()
        with patch('main.resolve_artist', return_value=None) as mock_resolve:
            resolve_calendar_ids(['A', 'B', 'C'], sp_raw=object(), cache=cache)
        assert mock_resolve.call_count == 3
