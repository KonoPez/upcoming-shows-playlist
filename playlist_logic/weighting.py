"""
Concert-proximity weighting and track-slot allocation.

Weights follow exponential decay with a 21-day half-life:
  - Concert tomorrow  → weight ≈ 1.00
  - Concert in 3 wks  → weight ≈ 0.50
  - Concert in 6 wks  → weight ≈ 0.25
  - Concert in 90 days → weight ≈ 0.056

Artists are allocated a proportional share of the target playlist size.
Artists too far away to receive even the minimum slot count are excluded —
this implements the "don't force in artists from the end of the 90-day window
if near-term concerts already fill the list" requirement.

Hamilton's method (largest remainder) is used to distribute rounding errors
without exceeding the target size.
"""

import math
import logging
from datetime import date

from sources.models import Concert

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 21.0
_LAMBDA = math.log(2) / HALF_LIFE_DAYS
HEADLINER_BONUS = 1.5   # headliners receive 1.5× the base proximity weight vs openers


def concert_weight(days_until: int) -> float:
    """
    Exponential weight for a concert that is `days_until` days away.
    Returns 0.0 for concerts in the past (days_until <= 0).
    """
    if days_until <= 0:
        return 0.0
    return math.exp(-_LAMBDA * days_until)


def compute_artist_weights(
    artist_concerts: dict[str, list[Concert]], today: date
) -> dict[str, float]:
    """
    Compute a raw weight for each artist.

    artist_concerts: {spotify_artist_id: [Concert, ...]}
      Each entry is one concert appearance. An artist with two concerts —
      one in 10 days and one in 40 days — gets the *sum* of both weights.
      Headliner slots (is_opener=False) receive a HEADLINER_BONUS multiplier
      so headliners claim a proportionally larger share than supporting acts.
      An artist can be a headliner at one show and an opener at another; the
      bonus is applied per concert, not per artist.

    Returns: {spotify_artist_id: raw_weight}
    """
    weights = {}
    for artist_id, concerts in artist_concerts.items():
        total = sum(
            concert_weight(c.days_until(today)) * (1 if c.is_opener else HEADLINER_BONUS)
            for c in concerts
        )
        if total > 0.0:
            weights[artist_id] = total
    return weights


def allocate_slots(
    weights: dict[str, float],
    target_duration_ms: int,
    min_duration_ms: int = 0,
) -> dict[str, int]:
    """
    Distribute `target_duration_ms` of playlist time across artists proportional
    to their weights.

    Artists whose exact proportional share is below `min_duration_ms` are
    excluded — a far-away concert doesn't justify playlist space when near-term
    concerts already fill the target.

    Uses Hamilton's method (largest remainder) so the budgets sum to
    target_duration_ms exactly.

    Returns: {spotify_artist_id: duration_budget_ms}  (only artists with budget > 0)
    """
    if not weights:
        return {}

    total_weight = sum(weights.values())
    if total_weight == 0.0:
        return {}

    # Exact proportional allocation
    exact = {aid: (w / total_weight) * target_duration_ms for aid, w in weights.items()}

    # Exclude artists whose share is too small to justify the minimum
    qualified = {aid: e for aid, e in exact.items() if e >= min_duration_ms}

    if not qualified:
        top = max(weights, key=weights.__getitem__)
        return {top: target_duration_ms}

    # Recompute proportions over qualified artists only
    q_weight_total = sum(weights[a] for a in qualified)
    exact_q = {aid: (weights[aid] / q_weight_total) * target_duration_ms for aid in qualified}

    # Floor each value (enforce min_duration_ms floor)
    floors = {aid: max(min_duration_ms, math.floor(v)) for aid, v in exact_q.items()}

    # Hamilton's method: distribute remaining ms by largest fractional remainder
    remainders = {aid: exact_q[aid] - math.floor(exact_q[aid]) for aid in exact_q}
    leftover = target_duration_ms - sum(floors.values())

    for aid, _ in sorted(remainders.items(), key=lambda x: x[1], reverse=True):
        if leftover <= 0:
            break
        floors[aid] += 1
        leftover -= 1

    # If min_duration_ms floors pushed us over budget, trim from lightest artists first
    overage = sum(floors.values()) - target_duration_ms
    for aid in sorted(floors, key=weights.get):
        if overage <= 0:
            break
        trim = min(floors[aid] - min_duration_ms, overage)
        floors[aid] -= trim
        overage -= trim

    logger.debug('Duration budget allocation:')
    for aid, budget in sorted(floors.items(), key=lambda x: -x[1]):
        logger.debug(f'  {aid}: {budget // 60_000}m budget (weight={weights.get(aid, 0):.3f})')

    return floors
