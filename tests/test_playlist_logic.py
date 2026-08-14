"""
Tests for playlist_logic — pure functions, no external dependencies.
"""

import math
from datetime import date, timedelta

import pytest

from sources.models import Concert, Track, normalize_track_name
from playlist_logic.weighting import (
    HALF_LIFE_DAYS,
    HEADLINER_BONUS,
    allocate_slots,
    compute_artist_weights,
    concert_weight,
)
from playlist_logic.scoring import (
    LASTFM_W,
    RECENCY_WINDOW_DAYS,
    _familiarity,
    _parse_release_date,
    _recency_score,
    score_track,
    select_tracks_for_artist,
)

_TODAY = date(2024, 1, 1)


def _concert(days_until: int, is_opener: bool = False) -> Concert:
    """Build a minimal Concert object a fixed number of days from _TODAY."""
    return Concert(
        event_name='Test Show',
        artist_name='Artist',
        event_date=_TODAY + timedelta(days=days_until),
        venue='Venue',
        source='test',
        is_opener=is_opener,
    )


# ── normalize_track_name ──────────────────────────────────────────────────────

class TestNormalizeTrackName:
    def test_dash_live_variant_stripped(self):
        assert normalize_track_name('Dancers - Live at Bush Hall') == 'dancers'

    def test_paren_live_variant_stripped(self):
        assert normalize_track_name('Song (Live at Glastonbury)') == 'song'

    def test_paren_acoustic_variant_stripped(self):
        assert normalize_track_name('Song (Acoustic Version)') == 'song'

    def test_plain_name_unchanged(self):
        assert normalize_track_name('Concorde') == 'concorde'

    def test_uppercase_is_lowercased(self):
        assert normalize_track_name('CONCORDE') == 'concorde'

    def test_live_wire_not_stripped(self):
        # "Live" appears in the title itself, not as a parenthetical/dash
        # variant suffix, so it must be left intact.
        assert normalize_track_name('Live Wire') == 'live wire'

    def test_dash_demo_variant_stripped(self):
        assert normalize_track_name('Song - Demo') == 'song'


# ── concert_weight ────────────────────────────────────────────────────────────

class TestConcertWeight:
    def test_past_concert_is_zero(self):
        assert concert_weight(-1) == 0.0
        assert concert_weight(-100) == 0.0

    def test_today_is_peak_weight(self):
        assert concert_weight(0) == 1.0

    def test_today_outranks_tomorrow_past_is_zero(self):
        # Regression: a concert happening today must be strictly more
        # proximate than one tomorrow, and a concert one day in the past
        # must be weightless — today is NOT "already over".
        assert concert_weight(0) > concert_weight(1)
        assert concert_weight(-1) == 0.0

    def test_tomorrow_is_near_one(self):
        assert concert_weight(1) > 0.95

    def test_at_half_life_is_half(self):
        w = concert_weight(int(HALF_LIFE_DAYS))
        assert abs(w - 0.5) < 0.01

    def test_at_two_half_lives_is_quarter(self):
        w = concert_weight(int(HALF_LIFE_DAYS * 2))
        assert abs(w - 0.25) < 0.01

    def test_weight_strictly_decreases(self):
        assert concert_weight(0) > concert_weight(1) > concert_weight(10) > concert_weight(30) > concert_weight(90)


# ── compute_artist_weights ────────────────────────────────────────────────────

class TestComputeArtistWeights:
    def test_single_headliner_concert(self):
        weights = compute_artist_weights({'a1': [_concert(10)]}, _TODAY)
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_single_opener_concert(self):
        weights = compute_artist_weights({'a1': [_concert(10, is_opener=True)]}, _TODAY)
        assert abs(weights['a1'] - concert_weight(10)) < 1e-9

    def test_multiple_concerts_for_same_artist_are_summed(self):
        weights = compute_artist_weights({'a1': [_concert(10), _concert(20)]}, _TODAY)
        expected = (concert_weight(10) + concert_weight(20)) * HEADLINER_BONUS
        assert abs(weights['a1'] - expected) < 1e-9

    def test_past_concerts_excluded(self):
        weights = compute_artist_weights({'a1': [_concert(-1), _concert(-30)]}, _TODAY)
        assert 'a1' not in weights

    def test_todays_concert_is_included(self):
        # A concert happening today is the most proximate possible — it must
        # NOT be treated the same as a past show.
        weights = compute_artist_weights({'a1': [_concert(0)]}, _TODAY)
        assert 'a1' in weights
        assert abs(weights['a1'] - concert_weight(0) * HEADLINER_BONUS) < 1e-9

    def test_mixed_past_and_future_only_sums_future(self):
        weights = compute_artist_weights({'a1': [_concert(-5), _concert(10)]}, _TODAY)
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_multiple_artists(self):
        weights = compute_artist_weights({'a1': [_concert(7)], 'a2': [_concert(30)]}, _TODAY)
        assert 'a1' in weights and 'a2' in weights
        assert weights['a1'] > weights['a2']   # closer concert → heavier weight

    def test_empty_input(self):
        assert compute_artist_weights({}, _TODAY) == {}

    def test_headliner_outweighs_opener_same_day(self):
        # Same concert day — headliner should have a larger weight than opener.
        weights = compute_artist_weights(
            {'h': [_concert(14)], 'o': [_concert(14, is_opener=True)]}, _TODAY
        )
        assert weights['h'] > weights['o']
        assert abs(weights['h'] / weights['o'] - HEADLINER_BONUS) < 1e-9

    def test_mixed_roles_bonus_applied_per_concert(self):
        # Artist headlining in 11 days, opening in 33 days.
        weights = compute_artist_weights(
            {'a': [_concert(11), _concert(33, is_opener=True)]}, _TODAY
        )
        expected = concert_weight(11) * HEADLINER_BONUS + concert_weight(33)
        assert abs(weights['a'] - expected) < 1e-9


def _manual_concert(days_until: int, event_name: str = 'Manual Show', is_opener: bool = False) -> Concert:
    """Build a manual-source Concert a fixed number of days from _TODAY."""
    return Concert(
        event_name=event_name,
        artist_name='Artist',
        event_date=_TODAY + timedelta(days=days_until),
        venue='Venue',
        source='manual',
        is_opener=is_opener,
    )


class TestComputeArtistWeightsManual:
    """Manual-source concerts carry no source-specific weighting: every
    appearance is weighted by proximity and role exactly as a calendar or
    Ticketmaster one is."""

    def test_single_manual_headliner_matches_non_manual_headliner(self):
        weights = compute_artist_weights({'a1': [_manual_concert(10)]}, _TODAY)
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_manual_bill_matches_identical_calendar_bill(self):
        # The same 1-headliner/2-opener bill, once manual and once from a
        # calendar, must produce identical weights — source is not a signal.
        bill = {'h': False, 'o1': True, 'o2': True}
        manual = compute_artist_weights(
            {aid: [_manual_concert(10, event_name='Fest', is_opener=op)]
             for aid, op in bill.items()},
            _TODAY,
        )
        calendar = compute_artist_weights(
            {aid: [_concert(10, is_opener=op)] for aid, op in bill.items()},
            _TODAY,
        )
        assert manual == calendar

    def test_manual_bill_members_keep_full_per_concert_weight(self):
        # One event (same date + event_name), 1 headliner + 2 openers, all
        # manual. Sharing a bill must not shrink anyone's weight.
        weights = compute_artist_weights({
            'h':  [_manual_concert(10, event_name='Fest', is_opener=False)],
            'o1': [_manual_concert(10, event_name='Fest', is_opener=True)],
            'o2': [_manual_concert(10, event_name='Fest', is_opener=True)],
        }, _TODAY)
        assert abs(weights['h'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9
        assert abs(weights['o1'] - concert_weight(10)) < 1e-9
        assert abs(weights['o2'] - concert_weight(10)) < 1e-9

    def test_sooner_manual_headliner_outweighs_later_calendar_headliner(self):
        # A manual show 51 days out and a calendar show 61 days out: the
        # sooner one must win, since proximity is the only thing separating them.
        weights = compute_artist_weights({
            'manual_hl':   [_manual_concert(51)],
            'calendar_hl': [_concert(61)],
        }, _TODAY)
        assert weights['manual_hl'] > weights['calendar_hl']

    def test_separate_manual_events_do_not_dilute_each_other(self):
        # Two distinct events (different event_name) on the same day, each
        # with a single manual artist — neither should be diluted by the other.
        weights = compute_artist_weights({
            'a1': [_manual_concert(10, event_name='Event A')],
            'a2': [_manual_concert(10, event_name='Event B')],
        }, _TODAY)
        assert abs(weights['a1'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9
        assert abs(weights['a2'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_past_manual_concert_excluded(self):
        weights = compute_artist_weights({'a1': [_manual_concert(-5)]}, _TODAY)
        assert 'a1' not in weights

    def test_todays_manual_concert_is_included(self):
        # A manual concert happening today is fully proximate, not "already
        # over" — it must be included, unlike a genuinely past manual concert.
        weights = compute_artist_weights({'a1': [_manual_concert(0)]}, _TODAY)
        assert 'a1' in weights
        assert abs(weights['a1'] - concert_weight(0) * HEADLINER_BONUS) < 1e-9


# ── allocate_slots ────────────────────────────────────────────────────────────

_TARGET_MS = 4_200_000  # 70 min — target budget used across allocation tests


class TestAllocateSlots:
    def test_total_equals_target(self):
        weights = {'a': 1.0, 'b': 0.5, 'c': 0.25}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert sum(slots.values()) == _TARGET_MS

    def test_heavier_artist_gets_more_budget(self):
        weights = {'heavy': 1.0, 'light': 0.2}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert slots['heavy'] > slots['light']

    def test_single_artist_gets_full_budget(self):
        slots = allocate_slots({'only': 1.0}, target_duration_ms=_TARGET_MS)
        assert slots == {'only': _TARGET_MS}

    def test_empty_weights_returns_empty(self):
        assert allocate_slots({}, target_duration_ms=_TARGET_MS) == {}

    def test_tiny_weight_artist_still_included(self):
        # No minimum-budget floor: every weighted artist keeps a slot, however
        # small its proportional share.
        weights = {'big': 100.0, 'tiny': 0.001}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert set(slots) == {'big', 'tiny'}
        assert sum(slots.values()) == _TARGET_MS

    def test_no_budget_wasted(self):
        # Hamilton's method must distribute the full target
        weights = {'a': 3.0, 'b': 2.0, 'c': 1.0}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert sum(slots.values()) == _TARGET_MS

    def test_two_equal_weight_artists_split_evenly(self):
        weights = {'a': 1.0, 'b': 1.0}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert slots['a'] == slots['b'] == _TARGET_MS // 2

    def test_zero_total_weight_returns_empty(self):
        assert allocate_slots({'a': 0.0, 'b': 0.0}, target_duration_ms=_TARGET_MS) == {}


# ── _parse_release_date ───────────────────────────────────────────────────────

class TestParseReleaseDate:
    def test_full_iso_date(self):
        assert _parse_release_date('2023-06-15') == date(2023, 6, 15)

    def test_year_month(self):
        assert _parse_release_date('2023-06') == date(2023, 6, 1)

    def test_year_only(self):
        assert _parse_release_date('2023') == date(2023, 1, 1)

    def test_invalid_date_falls_back_to_2000(self):
        assert _parse_release_date('not-a-date') == date(2000, 1, 1)

    def test_empty_string_falls_back(self):
        assert _parse_release_date('') == date(2000, 1, 1)


# ── _recency_score ────────────────────────────────────────────────────────────

class TestRecencyScore:
    TODAY = date(2024, 6, 1)

    def test_released_today_is_one(self):
        assert _recency_score(self.TODAY, self.TODAY) == 1.0

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



# ── _familiarity ──────────────────────────────────────────────────────────────

class TestFamiliarity:
    def test_unknown_track_is_zero(self):
        assert _familiarity('t1', {}, {}) == 0.0

    def test_api_score_used(self):
        assert _familiarity('t1', {'t1': 0.8}, {}) == 0.8

    def test_play_count_score(self):
        # 5 plays out of FAMILIAR_AT_N_PLAYS (10) → 0.5
        assert abs(_familiarity('t1', {}, {'t1': 5}) - 0.2) < 1e-9

    def test_play_count_at_cap_is_one(self):
        assert _familiarity('t1', {}, {'t1': 25}) == 1.0

    def test_play_count_above_cap_is_clamped(self):
        assert _familiarity('t1', {}, {'t1': 100}) == 1.0

    def test_max_of_api_and_play_history(self):
        # API says 0.3; play history says 0.7 → use 0.7
        assert abs(_familiarity('t1', {'t1': 0.2}, {'t1': 7}) - 0.28) < 1e-9



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

    def test_higher_lastfm_popularity_raises_score(self):
        lo = score_track(self._track(), {}, {}, self.TODAY, lastfm_scores={'test track': 0.1})
        hi = score_track(self._track(), {}, {}, self.TODAY, lastfm_scores={'test track': 0.9})
        assert hi > lo

    def test_lastfm_and_setlist_both_score_higher_than_either_alone(self):
        # Old track, unfamiliar: recency=0, novelty=1.0. Both signals at max.
        setlist_only = score_track(self._track(), {}, {}, self.TODAY,
                                   setlist_scores={'test track': 1.0})
        lastfm_only  = score_track(self._track(), {}, {}, self.TODAY,
                                   lastfm_scores={'test track': 1.0})
        both = score_track(self._track(), {}, {}, self.TODAY,
                           setlist_scores={'test track': 1.0},
                           lastfm_scores={'test track': 1.0})
        assert both > setlist_only
        assert both > lastfm_only


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

    def test_lastfm_popular_track_ranked_first(self):
        tracks = self._tracks(2)
        lastfm = {'track 2': 1.0}
        selected = select_tracks_for_artist(tracks, 1 * _TRACK_MS, {}, {}, self.TODAY,
                                            lastfm_scores=lastfm)
        assert selected[0].id == 't2'

    def test_does_not_overshoot_budget_by_a_whole_track(self):
        # Budget of 5.4 tracks: the 6th track would overshoot by 0.6 of a track,
        # while stopping at 5 undershoots by only 0.4 — so 5 is the closer fit.
        selected = select_tracks_for_artist(
            self._tracks(10), int(5.4 * _TRACK_MS), {}, {}, self.TODAY
        )
        assert len(selected) == 5

    def test_takes_track_that_lands_closer_to_budget(self):
        # Budget of 5.6 tracks: taking the 6th overshoots by 0.4 of a track,
        # which is closer than stopping at 5 and undershooting by 0.6.
        selected = select_tracks_for_artist(
            self._tracks(10), int(5.6 * _TRACK_MS), {}, {}, self.TODAY
        )
        assert len(selected) == 6

    def test_first_track_always_taken_despite_tiny_budget(self):
        # A budget far below one track still yields one track, so an artist
        # holding a slot is never silently dropped from the playlist.
        selected = select_tracks_for_artist(self._tracks(10), 1000, {}, {}, self.TODAY)
        assert len(selected) == 1

    def test_result_ordered_best_first(self):
        # Tracks with higher setlist frequency should be selected first
        tracks = self._tracks(5)
        setlist = {f'track {i}': i / 10 for i in range(1, 6)}   # track 5 → 0.5, track 1 → 0.1
        selected = select_tracks_for_artist(tracks, 3 * _TRACK_MS, {}, {}, self.TODAY,
                                            setlist_scores=setlist)
        ids = [t.id for t in selected]
        assert ids == ['t5', 't4', 't3']   # highest setlist frequency first

