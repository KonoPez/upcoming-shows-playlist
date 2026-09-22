"""
Tests for playlist_logic — pure functions, no external dependencies.
"""

import math
from datetime import date, timedelta

import pytest

from sources.models import Concert, Track
from playlist_logic.weighting import (
    FAMILIARITY_PENALTY,
    PLAY_LOG_W,
    TOP_SCORE_W,
    HALF_LIFE_DAYS,
    HEADLINER_BONUS,
    MIN_ARTIST_BUDGET_MS,
    allocate_slots,
    compute_artist_familiarity_scores,
    compute_artist_weights,
    concert_weight,
    novelty_multiplier,
)
from playlist_logic.scoring import (
    FAMILIAR_AT_N_PLAYS,
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


# ── novelty_multiplier ────────────────────────────────────────────────────────

class TestNoveltyMultiplier:
    def test_unfamiliar_artist_is_unpenalised(self):
        assert novelty_multiplier(0.0) == 1.0

    def test_maximum_familiarity_pays_the_full_penalty(self):
        assert abs(novelty_multiplier(1.0) - (1.0 - FAMILIARITY_PENALTY)) < 1e-9

    def test_strictly_decreasing_in_familiarity(self):
        values = [novelty_multiplier(f / 10) for f in range(11)]
        assert all(a > b for a, b in zip(values, values[1:]))

    def test_out_of_range_input_is_clamped(self):
        # A stray score outside 0–1 must not invert the weight or overshoot
        # the intended floor.
        assert novelty_multiplier(-5.0) == novelty_multiplier(0.0)
        assert novelty_multiplier(5.0) == novelty_multiplier(1.0)

    def test_never_zeroes_an_artist_out(self):
        assert novelty_multiplier(1.0) > 0.0


# ── compute_artist_familiarity_scores ─────────────────────────────────────────

class TestComputeArtistFamiliarityScores:
    def test_defaults_to_normalising_within_the_candidate_set(self):
        # Discovery's mode: the most-played candidate anchors the scale, so a
        # pool of barely-played unknowns still spreads across the range.
        scores = compute_artist_familiarity_scores(['a1', 'a2'], {}, {'a1': 2, 'a2': 0})
        assert scores['a1'] == 1.0
        assert scores['a2'] == 0.0

    def test_global_ceiling_keeps_a_lightly_played_artist_low(self):
        # Prep's mode: two lifetime plays is not "familiar" just because the
        # other artist on the bill has none.
        play_counts = {'a1': 2, 'a2': 0, 'heavy': 500}
        scores = compute_artist_familiarity_scores(
            ['a1', 'a2'], {}, play_counts, normalize_against=list(play_counts)
        )
        assert scores['a1'] < 0.25
        assert scores['a2'] == 0.0

    def test_global_ceiling_still_scores_the_most_played_artist_at_one(self):
        play_counts = {'a1': 500, 'other': 12}
        scores = compute_artist_familiarity_scores(
            ['a1'], {}, play_counts, normalize_against=list(play_counts)
        )
        assert abs(scores['a1'] - 1.0) < 1e-9

    def test_absent_from_top_lists_falls_back_to_play_history_alone(self):
        # Most of the discovery pool. A missing top-artist entry is "no
        # opinion", not a zero, so the play term is not diluted by it.
        play_counts = {'a1': 500, 'a2': 0}
        scores = compute_artist_familiarity_scores(['a1', 'a2'], {}, play_counts)
        assert abs(scores['a1'] - 1.0) < 1e-9
        assert scores['a2'] == 0.0

    def test_both_signals_contribute_when_both_are_present(self):
        # The complaint that motivated the blend: under `max`, a shared top
        # score made these two identical however differently they were played.
        play_counts = {'heavy': 200, 'light': 3, 'ceiling': 500}
        scores = compute_artist_familiarity_scores(
            ['heavy', 'light'],
            {'heavy': 0.8, 'light': 0.8},
            play_counts,
            normalize_against=list(play_counts),
        )
        assert scores['heavy'] > scores['light']

    def test_blend_matches_the_declared_weights(self):
        play_counts = {'a1': 24, 'ceiling': 499}
        scores = compute_artist_familiarity_scores(
            ['a1'], {'a1': 0.9}, play_counts, normalize_against=list(play_counts)
        )
        play_score = math.log(25) / math.log(500)
        expected = (TOP_SCORE_W * 0.9 + PLAY_LOG_W * play_score) / (TOP_SCORE_W + PLAY_LOG_W)
        assert abs(scores['a1'] - expected) < 1e-9

    def test_top_score_outweighs_play_history(self):
        # Spotify's ranking sees every device across the whole window; the local
        # log only sees what cron sampled, so it must not be the louder signal.
        assert TOP_SCORE_W > PLAY_LOG_W

    def test_a_top_ranked_artist_is_not_dragged_down_to_the_play_term(self):
        play_counts = {'a1': 0, 'ceiling': 500}
        scores = compute_artist_familiarity_scores(
            ['a1'], {'a1': 1.0}, play_counts, normalize_against=list(play_counts)
        )
        assert scores['a1'] >= TOP_SCORE_W

    def test_stays_within_the_unit_range(self):
        play_counts = {'a1': 500}
        for top in (0.0, 0.5, 1.0):
            scores = compute_artist_familiarity_scores(
                ['a1'], {'a1': top}, play_counts, normalize_against=list(play_counts)
            )
            assert 0.0 <= scores['a1'] <= 1.0


# ── compute_artist_weights (familiarity downweight) ───────────────────────────

class TestArtistWeightsWithFamiliarity:
    def test_omitting_familiarity_leaves_weights_untouched(self):
        bill = {'a1': [_concert(10)], 'a2': [_concert(30, is_opener=True)]}
        assert compute_artist_weights(bill, _TODAY) == compute_artist_weights(bill, _TODAY, None)

    def test_empty_familiarity_leaves_weights_untouched(self):
        bill = {'a1': [_concert(10)], 'a2': [_concert(30, is_opener=True)]}
        assert compute_artist_weights(bill, _TODAY) == compute_artist_weights(bill, _TODAY, {})

    def test_familiar_artist_is_downweighted(self):
        bill = {'a1': [_concert(10)]}
        base = compute_artist_weights(bill, _TODAY)['a1']
        with_fam = compute_artist_weights(bill, _TODAY, {'a1': 1.0})['a1']
        assert abs(with_fam - base * novelty_multiplier(1.0)) < 1e-9

    def test_novel_artist_beats_familiar_one_at_equal_proximity(self):
        # The festival case: one event, one date, familiarity is the only
        # signal that can separate the bill.
        bill = {'known': [_concert(21)], 'unknown': [_concert(21)]}
        weights = compute_artist_weights(bill, _TODAY, {'known': 1.0, 'unknown': 0.0})
        assert weights['unknown'] > weights['known']

    def test_artist_missing_from_the_dict_is_treated_as_unfamiliar(self):
        bill = {'a1': [_concert(10)], 'a2': [_concert(10)]}
        weights = compute_artist_weights(bill, _TODAY, {'a1': 1.0})
        assert abs(weights['a2'] - concert_weight(10) * HEADLINER_BONUS) < 1e-9

    def test_penalty_applies_once_across_multiple_concerts(self):
        # The multiplier is a per-artist constant, so scaling the summed weight
        # must equal scaling each concert individually.
        bill = {'a1': [_concert(10), _concert(20, is_opener=True)]}
        expected = (concert_weight(10) * HEADLINER_BONUS + concert_weight(20)) \
            * novelty_multiplier(0.6)
        assert abs(compute_artist_weights(bill, _TODAY, {'a1': 0.6})['a1'] - expected) < 1e-9

    def test_proximity_still_dominates_familiarity(self):
        # The contract that keeps this a marginal signal: a show you are ready
        # for tonight still outranks an unknown act a month out. If someone
        # raises FAMILIARITY_PENALTY far enough to break this, it stops being
        # a tiebreaker and starts overriding the calendar.
        weights = compute_artist_weights(
            {'known_tonight': [_concert(0)], 'unknown_later': [_concert(30)]},
            _TODAY,
            {'known_tonight': 1.0, 'unknown_later': 0.0},
        )
        assert weights['known_tonight'] > weights['unknown_later']

    def test_billing_stays_decisive_at_maximum_familiarity(self):
        # A headliner the user knows cold must still outweigh a total unknown
        # opening the same night. FAMILIARITY_PENALTY is deliberately set so the
        # swing stays under HEADLINER_BONUS: at 0.35 the crossover fell at
        # familiarity 0.952 and a real bill crossed it (Geese, at 0.996,
        # dropped below its own opener). 0.33 puts the crossover at 1.010,
        # outside the clamped input range, so it cannot be reached at all.
        weights = compute_artist_weights(
            {'headliner': [_concert(14)], 'opener': [_concert(14, is_opener=True)]},
            _TODAY,
            {'headliner': 1.0, 'opener': 0.0},
        )
        assert weights['headliner'] > weights['opener']

    def test_familiarity_swing_cannot_reach_the_headliner_bonus(self):
        # The same contract stated on the constants, so a change to either one
        # fails here rather than silently inverting a bill.
        assert HEADLINER_BONUS * novelty_multiplier(1.0) > 1.0

    def test_past_concert_stays_excluded_regardless_of_familiarity(self):
        weights = compute_artist_weights({'a1': [_concert(-5)]}, _TODAY, {'a1': 0.0})
        assert 'a1' not in weights


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


# ── allocate_slots (min_budget_ms floor) ──────────────────────────────────────

class TestAllocateSlotsMinBudget:
    def test_artist_below_floor_is_dropped(self):
        # 'tiny' earns a fraction of a second out of 70 minutes — not enough to
        # justify the whole track it would otherwise be handed.
        weights = {'big': 100.0, 'tiny': 0.001}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert set(slots) == {'big'}

    def test_artist_at_floor_is_kept(self):
        # Weighted so 'small' lands just above MIN_ARTIST_BUDGET_MS.
        share = (MIN_ARTIST_BUDGET_MS + 1_000) / _TARGET_MS
        weights = {'big': 1.0 - share, 'small': share}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert set(slots) == {'big', 'small'}
        assert slots['small'] >= MIN_ARTIST_BUDGET_MS

    def test_freed_budget_is_redistributed_to_survivors(self):
        weights = {'a': 10.0, 'b': 10.0, 'tiny': 0.001}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert sum(slots.values()) == _TARGET_MS
        assert slots['a'] + slots['b'] == _TARGET_MS

    def test_survivors_keep_their_relative_shares(self):
        weights = {'heavy': 3.0, 'light': 1.0, 'tiny': 0.0001}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert slots['heavy'] == pytest.approx(3 * slots['light'], rel=1e-6)

    def test_borderline_artist_is_rescued_by_redistribution(self):
        # 'small' and 'smaller' both start below the floor, so dropping every
        # offender at once would lose them both. Evicting only the smallest and
        # redistributing lifts 'small' clear of the floor, so it keeps its slot.
        weights = {'a': 1.0, 'b': 1.0, 'small': 0.235, 'smaller': 0.230}
        # Scaled to the floor so this stays valid whatever the floor is set to.
        target = 10 * MIN_ARTIST_BUDGET_MS

        initial = allocate_slots(weights, target)
        assert initial['small'] < MIN_ARTIST_BUDGET_MS
        assert initial['smaller'] < initial['small']

        slots = allocate_slots(weights, target_duration_ms=target, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert set(slots) == {'a', 'b', 'small'}
        assert slots['small'] >= MIN_ARTIST_BUDGET_MS

    def test_evicts_lowest_allocation_first(self):
        # Of the two artists under the floor, the smaller one is the one to go.
        weights = {'a': 1.0, 'b': 1.0, 'small': 0.235, 'smaller': 0.230}
        slots = allocate_slots(weights, target_duration_ms=10 * MIN_ARTIST_BUDGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert 'smaller' not in slots
        assert 'small' in slots

    def test_every_survivor_still_clears_the_floor(self):
        # The loop only stops once the smallest survivor clears the floor.
        weights = {'a': 5.0, 'b': 1.0, 'c': 0.01, 'd': 0.005}
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert slots
        assert all(budget >= MIN_ARTIST_BUDGET_MS for budget in slots.values())

    def test_nobody_above_floor_keeps_the_heaviest_artist(self):
        # A 300-artist bill against a 60-second target: everyone is below the
        # floor, but an empty playlist would be worse than one artist.
        weights = {f'a{i}': 1.0 for i in range(300)}
        weights['headliner'] = 2.0
        slots = allocate_slots(weights, target_duration_ms=60_000, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert slots == {'headliner': 60_000}

    def test_no_artist_below_floor_leaves_allocation_untouched(self):
        weights = {'a': 1.0, 'b': 1.0, 'c': 1.0}
        assert (allocate_slots(weights, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS)
                == allocate_slots(weights, target_duration_ms=_TARGET_MS))

    def test_empty_weights_returns_empty(self):
        assert allocate_slots({}, target_duration_ms=_TARGET_MS, min_budget_ms=MIN_ARTIST_BUDGET_MS) == {}

    def test_single_artist_below_floor_still_gets_full_budget(self):
        # One artist, tiny target: dropping them would leave nothing at all.
        slots = allocate_slots({'only': 1.0}, target_duration_ms=10_000, min_budget_ms=MIN_ARTIST_BUDGET_MS)
        assert slots == {'only': 10_000}


# ── familiarity effects on allocation ─────────────────────────────────────────

class TestFamiliarityInAllocation:
    def test_novel_artist_gets_the_larger_budget_at_equal_proximity(self):
        weights = compute_artist_weights(
            {'known': [_concert(21)], 'unknown': [_concert(21)]},
            _TODAY,
            {'known': 1.0, 'unknown': 0.0},
        )
        slots = allocate_slots(weights, target_duration_ms=_TARGET_MS)
        assert slots['unknown'] > slots['known']
        assert sum(slots.values()) == _TARGET_MS

    def test_familiar_artist_is_evicted_before_the_novel_one(self):
        # The crowded-calendar case from issue #3. Both marginal artists play
        # the same night in the same role; only familiarity separates them, and
        # the budget has room for exactly one of them above the floor.
        bill = {
            'near1': [_concert(0)],
            'near2': [_concert(0)],
            'near3': [_concert(0)],
            'zeta_familiar': [_concert(20)],
            'alpha_novel': [_concert(20)],
        }
        weights = compute_artist_weights(bill, _TODAY, {'zeta_familiar': 1.0, 'alpha_novel': 0.0})
        slots = allocate_slots(
            weights, target_duration_ms=1_000_000, min_budget_ms=MIN_ARTIST_BUDGET_MS
        )
        assert 'alpha_novel' in slots
        assert 'zeta_familiar' not in slots

    def test_survivors_still_clear_the_floor_and_sum_to_target(self):
        bill = {
            'near1': [_concert(0)],
            'near2': [_concert(0)],
            'near3': [_concert(0)],
            'zeta_familiar': [_concert(20)],
            'alpha_novel': [_concert(20)],
        }
        weights = compute_artist_weights(bill, _TODAY, {'zeta_familiar': 1.0, 'alpha_novel': 0.0})
        slots = allocate_slots(
            weights, target_duration_ms=1_000_000, min_budget_ms=MIN_ARTIST_BUDGET_MS
        )
        assert all(b >= MIN_ARTIST_BUDGET_MS for b in slots.values())
        assert sum(slots.values()) == 1_000_000


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

    def test_first_play_is_worth_the_most(self):
        # Play history is log-scaled: familiarity grows steeply over the first
        # few listens, then flattens. One play already buys a fifth of the way.
        assert abs(_familiarity('t1', {}, {'t1': 1}) - 0.2019) < 1e-4

    def test_returns_diminish_with_each_play(self):
        # The property the curve exists for: the Nth listen always teaches the
        # listener less than the (N-1)th did.
        scores = [_familiarity('t1', {}, {'t1': n}) for n in range(0, 12)]
        gains  = [b - a for a, b in zip(scores, scores[1:])]
        assert all(a > b for a, b in zip(gains, gains[1:]))

    def test_long_tail_keeps_climbing_past_the_early_plays(self):
        # Regression guard. An earlier revision divided a *natural* log by 1.5,
        # which hit the clamp at 3.5 plays and flattened everything above it —
        # 4 plays and 400 scored identically, erasing the tail this curve is
        # for. These must stay strictly ordered, and all below 1.0.
        s4  = _familiarity('t1', {}, {'t1': 4})
        s10 = _familiarity('t1', {}, {'t1': 10})
        s20 = _familiarity('t1', {}, {'t1': 20})
        assert s4 < s10 < s20 < 1.0

    def test_midpoint_is_reached_well_before_the_cap(self):
        # Log-scaled, so half familiarity arrives around 4–5 plays rather than
        # at half the cap the way the old linear form did.
        assert _familiarity('t1', {}, {'t1': 4}) < 0.5 < _familiarity('t1', {}, {'t1': 5})

    def test_play_count_at_cap_is_one(self):
        # The divisor is derived from the constant, so the cap lands exactly on
        # it rather than 0.6 plays past it. Retuning the constant moves this.
        assert _familiarity('t1', {}, {'t1': FAMILIAR_AT_N_PLAYS}) == 1.0

    def test_just_below_the_cap_is_not_yet_one(self):
        assert _familiarity('t1', {}, {'t1': FAMILIAR_AT_N_PLAYS - 1}) < 1.0

    def test_play_count_above_cap_is_clamped(self):
        assert _familiarity('t1', {}, {'t1': 100}) == 1.0

    def test_api_score_wins_when_higher(self):
        # 5 plays scores ~0.52; the API's 0.9 is the stronger claim.
        assert _familiarity('t1', {'t1': 0.9}, {'t1': 5}) == 0.9

    def test_play_history_wins_when_higher(self):
        # 7 plays scores ~0.61, above the API's 0.2.
        assert abs(_familiarity('t1', {'t1': 0.2}, {'t1': 7}) - 0.6055) < 1e-4


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

