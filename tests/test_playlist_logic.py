"""
Tests for playlist_logic — pure functions, no external dependencies.
"""

import math
from datetime import date, timedelta

import pytest

from playlist_logic.weighting import (
    HALF_LIFE_DAYS,
    allocate_slots,
    compute_artist_weights,
    concert_weight,
)
from playlist_logic.scoring import (
    RECENCY_WINDOW_DAYS,
    _familiarity,
    _interleave_albums,
    _parse_release_date,
    _recency_score,
    score_track,
    select_tracks_for_artist,
)


# ── concert_weight ────────────────────────────────────────────────────────────

class TestConcertWeight:
    def test_past_concert_is_zero(self):
        assert concert_weight(0) == 0.0
        assert concert_weight(-1) == 0.0
        assert concert_weight(-100) == 0.0

    def test_tomorrow_is_near_one(self):
        assert concert_weight(1) > 0.95

    def test_at_half_life_is_half(self):
        w = concert_weight(int(HALF_LIFE_DAYS))
        assert abs(w - 0.5) < 0.01

    def test_at_two_half_lives_is_quarter(self):
        w = concert_weight(int(HALF_LIFE_DAYS * 2))
        assert abs(w - 0.25) < 0.01

    def test_weight_strictly_decreases(self):
        assert concert_weight(1) > concert_weight(10) > concert_weight(30) > concert_weight(90)


# ── compute_artist_weights ────────────────────────────────────────────────────

class TestComputeArtistWeights:
    def test_single_artist_single_concert(self):
        weights = compute_artist_weights({'a1': [10]})
        assert abs(weights['a1'] - concert_weight(10)) < 1e-9

    def test_multiple_concerts_for_same_artist_are_summed(self):
        weights = compute_artist_weights({'a1': [10, 20]})
        expected = concert_weight(10) + concert_weight(20)
        assert abs(weights['a1'] - expected) < 1e-9

    def test_past_concerts_excluded(self):
        weights = compute_artist_weights({'a1': [0, -1, -30]})
        assert 'a1' not in weights

    def test_mixed_past_and_future_only_sums_future(self):
        weights = compute_artist_weights({'a1': [-5, 10]})
        assert abs(weights['a1'] - concert_weight(10)) < 1e-9

    def test_multiple_artists(self):
        weights = compute_artist_weights({'a1': [7], 'a2': [30]})
        assert 'a1' in weights and 'a2' in weights
        assert weights['a1'] > weights['a2']   # closer concert → heavier weight

    def test_empty_input(self):
        assert compute_artist_weights({}) == {}


# ── allocate_slots ────────────────────────────────────────────────────────────

class TestAllocateSlots:
    def test_total_equals_target(self):
        weights = {'a': 1.0, 'b': 0.5, 'c': 0.25}
        slots = allocate_slots(weights, target_size=20, min_slots=2)
        assert sum(slots.values()) == 20

    def test_all_included_artists_meet_min_slots(self):
        weights = {'a': 1.0, 'b': 0.5, 'c': 0.25}
        slots = allocate_slots(weights, target_size=20, min_slots=2)
        assert all(v >= 2 for v in slots.values())

    def test_heavier_artist_gets_more_slots(self):
        weights = {'heavy': 1.0, 'light': 0.2}
        slots = allocate_slots(weights, target_size=20, min_slots=2)
        assert slots['heavy'] > slots['light']

    def test_single_artist_gets_all_slots(self):
        slots = allocate_slots({'only': 1.0}, target_size=15, min_slots=2)
        assert slots == {'only': 15}

    def test_empty_weights_returns_empty(self):
        assert allocate_slots({}, target_size=20) == {}

    def test_artist_below_proportional_threshold_excluded(self):
        # 'tiny' proportional share ≪ min_slots → should be dropped
        weights = {'big': 100.0, 'tiny': 0.001}
        slots = allocate_slots(weights, target_size=10, min_slots=2)
        assert 'tiny' not in slots

    def test_no_slots_wasted(self):
        # Hamilton's method must distribute every slot
        weights = {'a': 3.0, 'b': 2.0, 'c': 1.0}
        slots = allocate_slots(weights, target_size=17, min_slots=2)
        assert sum(slots.values()) == 17

    def test_two_equal_weight_artists_split_evenly(self):
        weights = {'a': 1.0, 'b': 1.0}
        slots = allocate_slots(weights, target_size=10, min_slots=2)
        assert slots['a'] == slots['b'] == 5


# ── _parse_release_date ───────────────────────────────────────────────────────

class TestParseReleaseDate:
    def test_full_iso_date(self):
        assert _parse_release_date('2023-06-15', 'day') == date(2023, 6, 15)

    def test_year_month(self):
        assert _parse_release_date('2023-06', 'month') == date(2023, 6, 1)

    def test_year_only(self):
        assert _parse_release_date('2023', 'year') == date(2023, 1, 1)

    def test_invalid_date_falls_back_to_2000(self):
        assert _parse_release_date('not-a-date', 'day') == date(2000, 1, 1)

    def test_empty_string_falls_back(self):
        assert _parse_release_date('', 'day') == date(2000, 1, 1)


# ── _recency_score ────────────────────────────────────────────────────────────

class TestRecencyScore:
    TODAY = date(2024, 6, 1)

    def test_released_today_is_one(self):
        assert _recency_score(self.TODAY, self.TODAY) == 1.0

    def test_future_release_is_one(self):
        future = self.TODAY + timedelta(days=30)
        assert _recency_score(future, self.TODAY) == 1.0

    def test_old_release_is_zero(self):
        old = date(2000, 1, 1)
        assert _recency_score(old, self.TODAY) == 0.0

    def test_at_window_boundary_is_zero(self):
        boundary = self.TODAY - timedelta(days=RECENCY_WINDOW_DAYS)
        assert _recency_score(boundary, self.TODAY) == 0.0

    def test_halfway_through_window_is_half(self):
        halfway = self.TODAY - timedelta(days=RECENCY_WINDOW_DAYS // 2)
        score = _recency_score(halfway, self.TODAY)
        assert abs(score - 0.5) < 0.01

    def test_score_decreases_as_release_ages(self):
        s1 = _recency_score(self.TODAY - timedelta(days=30), self.TODAY)
        s2 = _recency_score(self.TODAY - timedelta(days=200), self.TODAY)
        s3 = _recency_score(self.TODAY - timedelta(days=400), self.TODAY)
        assert s1 > s2 > s3


# ── _familiarity ──────────────────────────────────────────────────────────────

class TestFamiliarity:
    def test_unknown_track_is_zero(self):
        assert _familiarity('t1', {}, {}) == 0.0

    def test_api_score_used(self):
        assert _familiarity('t1', {'t1': 0.8}, {}) == 0.8

    def test_play_count_score(self):
        # 5 plays out of FAMILIAR_AT_N_PLAYS (10) → 0.5
        assert abs(_familiarity('t1', {}, {'t1': 5}) - 0.5) < 1e-9

    def test_play_count_at_cap_is_one(self):
        assert _familiarity('t1', {}, {'t1': 10}) == 1.0

    def test_play_count_above_cap_is_clamped(self):
        assert _familiarity('t1', {}, {'t1': 100}) == 1.0

    def test_max_of_api_and_play_history(self):
        # API says 0.3; play history says 0.7 → use 0.7
        assert abs(_familiarity('t1', {'t1': 0.3}, {'t1': 7}) - 0.7) < 1e-9

    def test_api_wins_when_higher(self):
        # API says 0.9; play history says 0.2 → use 0.9
        assert _familiarity('t1', {'t1': 0.9}, {'t1': 2}) == 0.9


# ── score_track ───────────────────────────────────────────────────────────────

class TestScoreTrack:
    TODAY = date(2024, 1, 1)

    def _track(self, popularity=50, release_date='2000-01-01', track_id='t1'):
        return {
            'id': track_id,
            'name': 'Test Track',
            'popularity': popularity,
            'release_date': release_date,
            'release_date_precision': 'day',
        }

    def test_score_in_unit_range(self):
        score = score_track(self._track(), {}, {}, self.TODAY)
        assert 0.0 <= score <= 1.0

    def test_higher_popularity_raises_score(self):
        lo = score_track(self._track(popularity=10), {}, {}, self.TODAY)
        hi = score_track(self._track(popularity=90), {}, {}, self.TODAY)
        assert hi > lo

    def test_familiar_track_scores_lower(self):
        track = self._track()
        unfamiliar = score_track(track, {}, {}, self.TODAY)
        familiar = score_track(track, {'t1': 1.0}, {}, self.TODAY)
        assert unfamiliar > familiar

    def test_recent_release_scores_higher_than_old(self):
        # Release within the recency window should outscore a very old release
        old = score_track(self._track(release_date='2010-01-01'), {}, {}, self.TODAY)
        recent = score_track(self._track(release_date='2023-09-01'), {}, {}, self.TODAY)
        assert recent > old

    def test_fully_familiar_maximum_penalty(self):
        # A track with familiarity=1.0 gets novelty=0.0
        fully_familiar = score_track(self._track(), {'t1': 1.0}, {}, self.TODAY)
        unfamiliar = score_track(self._track(), {}, {}, self.TODAY)
        assert unfamiliar > fully_familiar


# ── select_tracks_for_artist ──────────────────────────────────────────────────

class TestSelectTracksForArtist:
    TODAY = date(2024, 1, 1)

    def _tracks(self, n, release_date='2000-01-01'):
        """Create n tracks with ascending popularity (t1=pop10 … tN=popN*10)."""
        return [
            {
                'id': f't{i}',
                'name': f'Track {i}',
                'popularity': i * 10,
                'release_date': release_date,
                'release_date_precision': 'day',
            }
            for i in range(1, n + 1)
        ]

    def test_returns_requested_count(self):
        selected = select_tracks_for_artist(self._tracks(10), 5, {}, {}, self.TODAY)
        assert len(selected) == 5

    def test_fewer_tracks_than_slots_returns_all(self):
        selected = select_tracks_for_artist(self._tracks(3), 10, {}, {}, self.TODAY)
        assert len(selected) == 3

    def test_empty_tracks_returns_empty(self):
        assert select_tracks_for_artist([], 5, {}, {}, self.TODAY) == []

    def test_zero_slots_returns_empty(self):
        assert select_tracks_for_artist(self._tracks(5), 0, {}, {}, self.TODAY) == []

    def test_unfamiliar_high_popularity_track_wins(self):
        # t1–t4 are fully familiar; t5 is unknown → t5 should rank first
        tracks = self._tracks(5)
        familiarity = {f't{i}': 1.0 for i in range(1, 5)}   # t1–t4 familiar
        selected = select_tracks_for_artist(tracks, 1, familiarity, {}, self.TODAY)
        assert selected[0]['id'] == 't5'

    def test_result_ordered_best_first(self):
        # With no familiarity signal, highest popularity should be selected first
        tracks = self._tracks(5)
        selected = select_tracks_for_artist(tracks, 3, {}, {}, self.TODAY)
        popularities = [t['popularity'] for t in selected]
        assert popularities == sorted(popularities, reverse=True)


# ── _interleave_albums ────────────────────────────────────────────────────────

def _make_track(tid: str, album_id: str, release_date: str, popularity: int = 50) -> dict:
    return {
        'id': tid,
        'name': f'Track {tid}',
        'popularity': popularity,
        'release_date': release_date,
        'release_date_precision': 'day',
        'album_id': album_id,
        'album_name': f'Album {album_id}',
    }


class TestInterleaveAlbums:
    def test_single_album_unchanged(self):
        tracks = [_make_track(f't{i}', 'a1', '2024-01-01') for i in range(4)]
        assert _interleave_albums(tracks) == tracks

    def test_empty_unchanged(self):
        assert _interleave_albums([]) == []

    def test_two_albums_alternate(self):
        a_tracks = [_make_track(f'a{i}', 'albumA', '2024-01-01') for i in range(3)]
        b_tracks = [_make_track(f'b{i}', 'albumB', '2023-01-01') for i in range(3)]
        result = _interleave_albums(a_tracks + b_tracks)
        album_ids = [t['album_id'] for t in result]
        for i in range(len(album_ids) - 1):
            assert album_ids[i] != album_ids[i + 1]

    def test_newer_album_leads(self):
        old = [_make_track('o1', 'old', '2020-01-01')]
        new = [_make_track('n1', 'new', '2024-01-01')]
        result = _interleave_albums(old + new)
        assert result[0]['album_id'] == 'new'

    def test_unequal_album_sizes_minimizes_consecutive_pairs(self):
        # 3 from A, 1 from B — some consecutive pairs are unavoidable (best is [A,B,A,A]),
        # but B should be interleaved early rather than left at the end.
        a_tracks = [_make_track(f'a{i}', 'albumA', '2024-01-01') for i in range(3)]
        b_tracks = [_make_track('b0', 'albumB', '2023-01-01')]
        result = _interleave_albums(a_tracks + b_tracks)
        album_ids = [t['album_id'] for t in result]
        consecutive_pairs = sum(
            1 for i in range(len(album_ids) - 1) if album_ids[i] == album_ids[i + 1]
        )
        # Theoretical minimum for a 3:1 split is 1 — verify we achieve it
        assert consecutive_pairs <= 1


# ── select_tracks_for_artist — album cap + interleaving ───────────────────────

class TestSelectTracksAlbumCap:
    TODAY = date(2024, 1, 1)

    def test_cap_fills_remaining_from_other_album(self):
        # Album A: 8 tracks (high pop); Album B: 4 tracks (lower pop)
        # Request 6 with cap=4 → 4 from A, 2 from B
        tracks_a = [_make_track(f'a{i}', 'albumA', '2024-01-01', popularity=80)
                    for i in range(8)]
        tracks_b = [_make_track(f'b{i}', 'albumB', '2023-01-01', popularity=30)
                    for i in range(4)]
        selected = select_tracks_for_artist(
            tracks_a + tracks_b, 6, {}, {}, self.TODAY, max_per_album=4
        )
        assert len(selected) == 6
        assert sum(1 for t in selected if t['album_id'] == 'albumA') == 4
        assert sum(1 for t in selected if t['album_id'] == 'albumB') == 2

    def test_no_consecutive_same_album(self):
        # Equal-score tracks across two albums — output must alternate
        tracks_a = [_make_track(f'a{i}', 'albumA', '2024-01-01', popularity=50)
                    for i in range(4)]
        tracks_b = [_make_track(f'b{i}', 'albumB', '2023-01-01', popularity=50)
                    for i in range(4)]
        selected = select_tracks_for_artist(tracks_a + tracks_b, 8, {}, {}, self.TODAY)
        album_ids = [t['album_id'] for t in selected]
        for i in range(len(album_ids) - 1):
            assert album_ids[i] != album_ids[i + 1], \
                f'Consecutive same album at positions {i},{i+1}: {album_ids}'

    def test_cap_zero_popularity_scenario(self):
        # Simulates Good Kid / BCNR: all popularity=0, newest album wins on recency.
        # Three albums ensure enough supply for 9 slots despite cap=4.
        # Cap should prevent any single album from claiming all slots.
        tracks_2026 = [_make_track(f'n{i}', 'new_album', '2026-01-01', popularity=0)
                       for i in range(9)]
        tracks_2023 = [_make_track(f'm{i}', 'mid_album', '2023-06-01', popularity=0)
                       for i in range(9)]
        tracks_2020 = [_make_track(f'o{i}', 'old_album', '2020-01-01', popularity=0)
                       for i in range(9)]
        selected = select_tracks_for_artist(
            tracks_2026 + tracks_2023 + tracks_2020, 9, {}, {}, self.TODAY, max_per_album=4
        )
        assert len(selected) == 9
        from_new = sum(1 for t in selected if t['album_id'] == 'new_album')
        assert from_new <= 4   # newest album is capped; older albums fill the rest
