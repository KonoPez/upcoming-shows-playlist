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
        assert score_artist_enjoyment(0.8, None) == pytest.approx(0.8)

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

    def test_similarity_raises_score(self):
        without = score_artist_enjoyment(0.0, None, similarity=None)
        with_sim = score_artist_enjoyment(0.0, None, similarity=1.0)
        assert with_sim > without

    def test_all_three_signals_blended(self):
        from playlist_logic.discovery_weighting import SIMILARITY_W
        fam, pop, sim = 1.0, 1.0, 1.0
        total_w = FAMILIARITY_W + POPULARITY_W + SIMILARITY_W
        expected = (FAMILIARITY_W + POPULARITY_W + SIMILARITY_W) / total_w
        assert score_artist_enjoyment(fam, pop, sim) == pytest.approx(expected)

    def test_all_three_full_scores_to_one(self):
        assert score_artist_enjoyment(1.0, 1.0, 1.0) == pytest.approx(1.0)

    def test_normalization_preserves_relative_weight(self):
        # With only familiarity available, the result should equal familiarity exactly
        assert score_artist_enjoyment(0.6, None, None) == pytest.approx(0.6)


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


# ── LastFmClient.get_similar_artists ─────────────────────────────────────────

class TestGetSimilarArtists:
    def _client(self, stored=None):
        from sources.lastfm import LastFmClient
        cache = MagicMock()
        cache.get.return_value = stored
        return LastFmClient('fake-key', cache)

    def _response(self, pairs):
        """pairs: list of (name, match_score)"""
        return {
            'similarartists': {
                'artist': [{'name': n, 'match': str(m)} for n, m in pairs]
            }
        }

    def test_returns_name_weight_dict(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response([('Radiohead', '0.9'), ('Portishead', '0.7')])
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_similar_artists('Massive Attack')
        assert result == {'radiohead': pytest.approx(0.9), 'portishead': pytest.approx(0.7)}

    def test_names_are_lowercased(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response([('Nine Inch Nails', '0.8')])
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_similar_artists('Artist')
        assert 'nine inch nails' in result

    def test_returns_cached_without_api_call(self):
        cached = {'bon iver': 0.85}
        client = self._client(stored=cached)
        with patch('sources.lastfm.requests.get') as mock_get:
            result = client.get_similar_artists('Artist')
            mock_get.assert_not_called()
        assert result == cached

    def test_api_error_returns_empty(self):
        import requests as req
        with patch('sources.lastfm.requests.get', side_effect=req.RequestException('timeout')):
            result = self._client().get_similar_artists('Artist')
        assert result == {}

    def test_empty_response_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'similarartists': {'artist': []}}
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_similar_artists('Artist')
        assert result == {}

    def test_zero_weight_entries_excluded(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response([('Good Artist', '0.8'), ('Zero Artist', '0.0')])
        mock_resp.raise_for_status.return_value = None
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            result = self._client().get_similar_artists('Artist')
        assert 'zero artist' not in result

    def test_result_cached_after_fetch(self):
        from sources.lastfm import LastFmClient
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._response([('Some Artist', '0.5')])
        mock_resp.raise_for_status.return_value = None
        cache = MagicMock()
        cache.get.return_value = None
        client = LastFmClient('fake-key', cache)
        with patch('sources.lastfm.requests.get', return_value=mock_resp):
            client.get_similar_artists('Artist')
        cache.set.assert_called_once()


# ── compute_artist_similarity_scores ─────────────────────────────────────────

class TestComputeArtistSimilarityScores:
    from playlist_logic.discovery_weighting import compute_artist_similarity_scores

    def _run(self, candidate_names, taste_profile, similar_map):
        """
        similar_map: {artist_name: {similar_name_lower: weight}}
        """
        from playlist_logic.discovery_weighting import compute_artist_similarity_scores
        lastfm = MagicMock()
        lastfm.get_similar_artists.side_effect = lambda name: similar_map.get(name, {})
        return compute_artist_similarity_scores(candidate_names, taste_profile, lastfm)

    def test_unknown_artist_scores_zero(self):
        result = self._run(['Unknown'], taste_profile={}, similar_map={'Unknown': {}})
        assert result['Unknown'] == 0.0

    def test_artist_similar_to_known_scores_above_zero(self):
        taste = {'radiohead': 1.0}
        similar = {'Candidate': {'radiohead': 0.9}}
        result = self._run(['Candidate'], taste, similar)
        assert result['Candidate'] > 0.0

    def test_higher_taste_match_gives_higher_score(self):
        taste = {'radiohead': 1.0, 'portishead': 0.5}
        strong = {'Strong': {'radiohead': 0.9}}
        weak   = {'Weak':   {'portishead': 0.3}}
        r_strong = self._run(['Strong'], taste, strong)
        r_weak   = self._run(['Weak'],   taste, weak)
        assert r_strong['Strong'] >= r_weak['Weak']

    def test_scores_normalised_between_zero_and_one(self):
        taste = {'radiohead': 1.0}
        similar = {
            'A': {'radiohead': 0.9},
            'B': {'radiohead': 0.5},
            'C': {},
        }
        result = self._run(['A', 'B', 'C'], taste, similar)
        for v in result.values():
            assert 0.0 <= v <= 1.0

    def test_top_scorer_normalises_to_one(self):
        taste = {'radiohead': 1.0}
        similar = {'Best': {'radiohead': 1.0}, 'Worse': {'radiohead': 0.5}}
        result = self._run(['Best', 'Worse'], taste, similar)
        assert result['Best'] == pytest.approx(1.0)

    def test_all_zero_raw_scores_returns_all_zero(self):
        result = self._run(['A', 'B'], taste_profile={}, similar_map={'A': {}, 'B': {}})
        assert result == {'A': 0.0, 'B': 0.0}

    def test_get_similar_called_once_per_candidate(self):
        from playlist_logic.discovery_weighting import compute_artist_similarity_scores
        lastfm = MagicMock()
        lastfm.get_similar_artists.return_value = {}
        compute_artist_similarity_scores(['A', 'B', 'C'], {}, lastfm)
        assert lastfm.get_similar_artists.call_count == 3


# ── _build_discovery_description ─────────────────────────────────────────────

class TestBuildDiscoveryDescription:
    """Tests for main._build_discovery_description."""

    def _concert(self, artist_name, venue='The Venue', days=10, is_opener=False):
        return Concert(
            event_name=artist_name,
            artist_name=artist_name,
            event_date=_TODAY + timedelta(days=days),
            venue=venue,
            is_opener=is_opener,
            source='ticketmaster_discovery',
        )

    def _artist(self, name, days=10):
        return Artist(
            spotify_id=name,
            name=name,
            concerts=[self._concert(name, days=days, is_opener=False)],
        )

    def _run(self, artists: dict, all_concerts: list) -> str:
        from main import _build_discovery_description
        return _build_discovery_description(artists, all_concerts)

    def test_single_headliner_no_openers(self):
        artists = {'id1': self._artist('Headliner')}
        concerts = [self._concert('Headliner')]
        result = self._run(artists, concerts)
        assert result == f'{(_TODAY + timedelta(days=10)).strftime("%m/%d")} Headliner at The Venue'

    def test_headliner_with_selected_opener(self):
        opener = self._concert('Opener', is_opener=True)
        headliner = self._concert('Headliner', is_opener=False)
        artists = {
            'h': Artist(spotify_id='h', name='Headliner', concerts=[headliner]),
            'o': Artist(spotify_id='o', name='Opener', concerts=[opener]),
        }
        result = self._run(artists, [headliner, opener])
        date_str = (_TODAY + timedelta(days=10)).strftime('%m/%d')
        assert result == f'{date_str} Headliner (Openers: Opener) at The Venue'

    def test_unselected_opener_not_listed(self):
        """An opener not in the artists dict should not appear in the description."""
        headliner = self._concert('Headliner', is_opener=False)
        unselected_opener = self._concert('Unknown Opener', is_opener=True)
        artists = {'h': Artist(spotify_id='h', name='Headliner', concerts=[headliner])}
        result = self._run(artists, [headliner, unselected_opener])
        assert 'Unknown Opener' not in result
        assert 'Openers' not in result

    def test_selected_opener_unselected_headliner(self):
        """If only the opener was selected, headliner still appears for context."""
        headliner = self._concert('Big Act', is_opener=False)
        opener = self._concert('Small Act', is_opener=True)
        artists = {'o': Artist(spotify_id='o', name='Small Act', concerts=[opener])}
        result = self._run(artists, [headliner, opener])
        assert 'Big Act' in result
        assert 'Small Act' in result

    def test_multiple_events_sorted_by_date(self):
        c1 = self._concert('Artist A', days=20)
        c2 = self._concert('Artist B', days=5)
        artists = {
            'a': Artist(spotify_id='a', name='Artist A', concerts=[c1]),
            'b': Artist(spotify_id='b', name='Artist B', concerts=[c2]),
        }
        result = self._run(artists, [c1, c2])
        assert result.index('Artist B') < result.index('Artist A')

    def test_event_with_no_selected_artists_excluded(self):
        selected = self._concert('Selected', venue='Venue A')
        irrelevant = self._concert('Irrelevant', venue='Venue B')
        artists = {'s': Artist(spotify_id='s', name='Selected', concerts=[selected])}
        result = self._run(artists, [selected, irrelevant])
        assert 'Irrelevant' not in result

    def test_truncates_at_300_chars(self):
        # Build enough events to exceed 300 chars
        concerts = []
        artists = {}
        for i in range(10):
            name = f'Artist With A Very Long Name Number {i}'
            c = self._concert(name, venue='A Very Long Venue Name Indeed', days=i + 1)
            concerts.append(c)
            artists[str(i)] = Artist(spotify_id=str(i), name=name, concerts=[c])
        result = self._run(artists, concerts)
        assert len(result) <= 300

    def test_empty_artists_returns_empty_string(self):
        result = self._run({}, [])
        assert result == ''

    def test_multiple_openers_joined_with_comma(self):
        headliner = self._concert('Headliner', is_opener=False)
        op1 = self._concert('Opener 1', is_opener=True)
        op2 = self._concert('Opener 2', is_opener=True)
        artists = {
            'h':  Artist(spotify_id='h',  name='Headliner', concerts=[headliner]),
            'o1': Artist(spotify_id='o1', name='Opener 1',  concerts=[op1]),
            'o2': Artist(spotify_id='o2', name='Opener 2',  concerts=[op2]),
        }
        result = self._run(artists, [headliner, op1, op2])
        assert 'Opener 1, Opener 2' in result or 'Opener 2, Opener 1' in result
