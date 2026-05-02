"""
Tests for playlist_logic — pure functions, no external dependencies.
"""

import math
from datetime import date, timedelta

import pytest

from sources.models import Track
from playlist_logic.weighting import (
    HALF_LIFE_DAYS,
    HEADLINER_BONUS,
    ConcertSlot,
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
    def test_single_headliner_concert(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(10, False)]})
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_single_opener_concert(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(10, True)]})
        assert abs(weights['a1'] - concert_weight(10)) < 1e-9

    def test_multiple_concerts_for_same_artist_are_summed(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(10, False), ConcertSlot(20, False)]})
        expected = (concert_weight(10) + concert_weight(20)) * HEADLINER_BONUS
        assert abs(weights['a1'] - expected) < 1e-9

    def test_past_concerts_excluded(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(0, False), ConcertSlot(-1, False), ConcertSlot(-30, False)]})
        assert 'a1' not in weights

    def test_mixed_past_and_future_only_sums_future(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(-5, False), ConcertSlot(10, False)]})
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_multiple_artists(self):
        weights = compute_artist_weights({'a1': [ConcertSlot(7, False)], 'a2': [ConcertSlot(30, False)]})
        assert 'a1' in weights and 'a2' in weights
        assert weights['a1'] > weights['a2']   # closer concert → heavier weight

    def test_empty_input(self):
        assert compute_artist_weights({}) == {}

    def test_headliner_outweighs_opener_same_day(self):
        # Same concert day — headliner should have a larger weight than opener.
        weights = compute_artist_weights({'h': [ConcertSlot(14, False)], 'o': [ConcertSlot(14, True)]})
        assert weights['h'] > weights['o']
        assert abs(weights['h'] / weights['o'] - HEADLINER_BONUS) < 1e-9

    def test_mixed_roles_bonus_applied_per_concert(self):
        # Artist headlining in 11 days, opening in 33 days.
        weights = compute_artist_weights({'a': [ConcertSlot(11, False), ConcertSlot(33, True)]})
        expected = concert_weight(11) * HEADLINER_BONUS + concert_weight(33)
        assert abs(weights['a'] - expected) < 1e-9


# ── allocate_slots ────────────────────────────────────────────────────────────

_MIN_MS = 420_000    # 2 tracks × 3.5 min — minimum budget threshold in tests
_TARGET_MS = 4_200_000  # 70 min — target budget used across allocation tests


class TestAllocateSlots:
    def test_total_equals_target(self):
        weights = {'a': 1.0, 'b': 0.5, 'c': 0.25}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert sum(slots.values()) == _TARGET_MS

    def test_all_included_artists_meet_min_duration(self):
        weights = {'a': 1.0, 'b': 0.5, 'c': 0.25}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert all(v >= _MIN_MS for v in slots.values())

    def test_heavier_artist_gets_more_budget(self):
        weights = {'heavy': 1.0, 'light': 0.2}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert slots['heavy'] > slots['light']

    def test_single_artist_gets_full_budget(self):
        slots = allocate_slots({'only': 1.0}, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert slots == {'only': _TARGET_MS}

    def test_empty_weights_returns_empty(self):
        assert allocate_slots({}, target_duration_ms=_TARGET_MS) == {}

    def test_artist_below_proportional_threshold_excluded(self):
        # 'tiny' proportional share ≪ min_duration_ms → should be dropped
        weights = {'big': 100.0, 'tiny': 0.001}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert 'tiny' not in slots

    def test_no_budget_wasted(self):
        # Hamilton's method must distribute the full target
        weights = {'a': 3.0, 'b': 2.0, 'c': 1.0}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert sum(slots.values()) == _TARGET_MS

    def test_two_equal_weight_artists_split_evenly(self):
        weights = {'a': 1.0, 'b': 1.0}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_duration_ms=_MIN_MS)
        assert slots['a'] == slots['b'] == _TARGET_MS // 2


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

    def _track(self, release_date='2000-01-01', track_id='t1'):
        return Track(
            id=track_id,
            name='Test Track',
            release_date=release_date,
            release_date_precision='day',
            duration_ms=0,
            album_id='',
            album_name='',
        )

    def test_score_in_unit_range(self):
        score = score_track(self._track(), {}, {}, self.TODAY)
        assert 0.0 <= score <= 1.0

    def test_higher_setlist_frequency_raises_score(self):
        lo = score_track(self._track(), {}, {}, self.TODAY, setlist_scores={'test track': 0.1})
        hi = score_track(self._track(), {}, {}, self.TODAY, setlist_scores={'test track': 0.9})
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

_TRACK_MS = 210_000   # 3.5 min — duration assigned to each test track


class TestSelectTracksForArtist:
    TODAY = date(2024, 1, 1)

    def _tracks(self, n, release_date='2000-01-01'):
        """Create n tracks all sharing the same release date."""
        return [
            Track(
                id=f't{i}',
                name=f'Track {i}',
                release_date=release_date,
                release_date_precision='day',
                duration_ms=_TRACK_MS,
                album_id='',
                album_name='',
            )
            for i in range(1, n + 1)
        ]

    def test_returns_requested_count(self):
        selected = select_tracks_for_artist(self._tracks(10), 5 * _TRACK_MS, {}, {}, self.TODAY)
        assert len(selected) == 5

    def test_fewer_tracks_than_budget_returns_all(self):
        selected = select_tracks_for_artist(self._tracks(3), 10 * _TRACK_MS, {}, {}, self.TODAY)
        assert len(selected) == 3

    def test_empty_tracks_returns_empty(self):
        assert select_tracks_for_artist([], 5 * _TRACK_MS, {}, {}, self.TODAY) == []

    def test_zero_budget_returns_empty(self):
        assert select_tracks_for_artist(self._tracks(5), 0, {}, {}, self.TODAY) == []

    def test_unfamiliar_track_beats_familiar_tracks(self):
        # t1–t4 are fully familiar (novelty=0); t5 is unknown (novelty=1.0).
        # All tracks share an old release date so recency=0 for all — novelty
        # is the only differentiator, so t5 should rank first.
        tracks = self._tracks(5)
        familiarity = {f't{i}': 1.0 for i in range(1, 5)}   # t1–t4 familiar
        selected = select_tracks_for_artist(tracks, 1 * _TRACK_MS, familiarity, {}, self.TODAY)
        assert selected[0].id == 't5'

    def test_result_ordered_best_first(self):
        # Tracks with higher setlist frequency should be selected first
        tracks = self._tracks(5)
        setlist = {f'track {i}': i / 10 for i in range(1, 6)}   # track 5 → 0.5, track 1 → 0.1
        selected = select_tracks_for_artist(tracks, 3 * _TRACK_MS, {}, {}, self.TODAY,
                                            setlist_scores=setlist)
        ids = [t.id for t in selected]
        assert ids == ['t5', 't4', 't3']   # highest setlist frequency first


# ── _interleave_albums ────────────────────────────────────────────────────────

def _make_track(tid: str, album_id: str, release_date: str) -> Track:
    return Track(
        id=tid,
        name=f'Track {tid}',
        release_date=release_date,
        release_date_precision='day',
        album_id=album_id,
        album_name=f'Album {album_id}',
        duration_ms=_TRACK_MS,
    )


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
        album_ids = [t.album_id for t in result]
        for i in range(len(album_ids) - 1):
            assert album_ids[i] != album_ids[i + 1]

    def test_newer_album_leads(self):
        old = [_make_track('o1', 'old', '2020-01-01')]
        new = [_make_track('n1', 'new', '2024-01-01')]
        result = _interleave_albums(old + new)
        assert result[0].album_id == 'new'

    def test_unequal_album_sizes_minimizes_consecutive_pairs(self):
        # 3 from A, 1 from B — some consecutive pairs are unavoidable (best is [A,B,A,A]),
        # but B should be interleaved early rather than left at the end.
        a_tracks = [_make_track(f'a{i}', 'albumA', '2024-01-01') for i in range(3)]
        b_tracks = [_make_track('b0', 'albumB', '2023-01-01')]
        result = _interleave_albums(a_tracks + b_tracks)
        album_ids = [t.album_id for t in result]
        consecutive_pairs = sum(
            1 for i in range(len(album_ids) - 1) if album_ids[i] == album_ids[i + 1]
        )
        # Theoretical minimum for a 3:1 split is 1 — verify we achieve it
        assert consecutive_pairs <= 1


# ── select_tracks_for_artist — album cap + interleaving ───────────────────────

class TestSelectTracksAlbumCap:
    TODAY = date(2024, 1, 1)

    def test_cap_fills_remaining_from_other_album(self):
        # Album A: 8 tracks (more recent); Album B: 4 tracks (older)
        # Request 6 with cap=4 → 4 from A, 2 from B
        tracks_a = [_make_track(f'a{i}', 'albumA', '2024-01-01')
                    for i in range(8)]
        tracks_b = [_make_track(f'b{i}', 'albumB', '2023-01-01')
                    for i in range(4)]
        selected = select_tracks_for_artist(
            tracks_a + tracks_b, 6 * _TRACK_MS, {}, {}, self.TODAY, max_per_album=4
        )
        assert len(selected) == 6
        assert sum(1 for t in selected if t.album_id == 'albumA') == 4
        assert sum(1 for t in selected if t.album_id == 'albumB') == 2

    def test_no_consecutive_same_album(self):
        # Tracks across two albums — output must alternate
        tracks_a = [_make_track(f'a{i}', 'albumA', '2024-01-01')
                    for i in range(4)]
        tracks_b = [_make_track(f'b{i}', 'albumB', '2023-01-01')
                    for i in range(4)]
        selected = select_tracks_for_artist(tracks_a + tracks_b, 8 * _TRACK_MS, {}, {}, self.TODAY)
        album_ids = [t.album_id for t in selected]
        for i in range(len(album_ids) - 1):
            assert album_ids[i] != album_ids[i + 1], \
                f'Consecutive same album at positions {i},{i+1}: {album_ids}'

    def test_cap_zero_popularity_scenario(self):
        # Simulates Good Kid / BCNR: all popularity=0, newest album wins on recency.
        # Three albums ensure enough supply for 9 slots despite cap=4.
        # Cap should prevent any single album from claiming all slots.
        tracks_2026 = [_make_track(f'n{i}', 'new_album', '2026-01-01') for i in range(9)]
        tracks_2023 = [_make_track(f'm{i}', 'mid_album', '2023-06-01') for i in range(9)]
        tracks_2020 = [_make_track(f'o{i}', 'old_album', '2020-01-01') for i in range(9)]
        selected = select_tracks_for_artist(
            tracks_2026 + tracks_2023 + tracks_2020, 9 * _TRACK_MS, {}, {}, self.TODAY, max_per_album=4
        )
        assert len(selected) == 9
        from_new = sum(1 for t in selected if t.album_id == 'new_album')
        assert from_new <= 4   # newest album is capped; older albums fill the rest
