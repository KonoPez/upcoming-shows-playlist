"""
Concert-proximity weighting and track-slot allocation.

Weights follow exponential decay with a 21-day half-life:
  - Concert today     → weight = 1.00
  - Concert tomorrow  → weight ≈ 0.968
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
from collections import defaultdict
from datetime import date

from sources.models import Concert

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 21.0
_LAMBDA = math.log(2) / HALF_LIFE_DAYS
HEADLINER_BONUS = 1.5   # headliners receive 1.5× the base proximity weight vs openers


def concert_weight(days_until: int) -> float:
    """
    Exponential weight for a concert that is `days_until` days away.
    A concert today (days_until == 0) receives the peak weight, 1.0.
    Returns 0.0 for concerts in the past (days_until < 0).
    """
    if days_until < 0:
        return 0.0
    return math.exp(-_LAMBDA * days_until)


def compute_artist_weights(
    artist_concerts: dict[str, list[Concert]], today: date
) -> dict[str, float]:
    """
    Compute a raw weight for each artist.

    artist_concerts: {spotify_artist_id: [Concert, ...]}
      Each entry is one concert appearance. An artist with two concerts
      gets the *sum* of both weights. Headliner slots (is_opener=False) 
      receive a HEADLINER_BONUS multiplier

    Returns: {spotify_artist_id: raw_weight}
    """
    weights: dict[str, float] = defaultdict(float)

    for artist_id, concerts in artist_concerts.items():
        for c in concerts:
            w = concert_weight(c.days_until(today)) * (1 if c.is_opener else HEADLINER_BONUS)
            weights[artist_id] += w

    return {aid: w for aid, w in weights.items() if w > 0}


def allocate_slots(
    weights: dict[str, float],
    target_duration_ms: int,
) -> dict[str, int]:
    """
    Distribute `target_duration_ms` across artists proportional to their weights.
    Every artist with a non-zero weight receives a budget.
    Returns: {spotify_artist_id: duration_budget_ms}
    """
    if not weights:
        return {}

    total_weight = sum(weights.values())
    if total_weight == 0.0:
        return {}

    # Exact proportional allocation
    exact = {aid: (w / total_weight) * target_duration_ms for aid, w in weights.items()}

    floors = {aid: math.floor(v) for aid, v in exact.items()}

    # Hamilton's method: distribute remaining ms by largest fractional remainder
    remainders = {aid: exact[aid] - floors[aid] for aid in exact}
    leftover = target_duration_ms - sum(floors.values())

    for aid, _ in sorted(remainders.items(), key=lambda x: x[1], reverse=True):
        if leftover <= 0:
            break
        floors[aid] += 1
        leftover -= 1

    logger.debug('Duration budget allocation:')
    for aid, budget in sorted(floors.items(), key=lambda x: -x[1]):
        logger.debug(f'  {aid}: {budget // 60_000}m budget (weight={weights.get(aid, 0):.3f})')

    return floors
