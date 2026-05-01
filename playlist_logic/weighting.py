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
from typing import Dict, List

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 21.0
_LAMBDA = math.log(2) / HALF_LIFE_DAYS


def concert_weight(days_until: int) -> float:
    """
    Exponential weight for a concert that is `days_until` days away.
    Returns 0.0 for concerts in the past (days_until <= 0).
    """
    if days_until <= 0:
        return 0.0
    return math.exp(-_LAMBDA * days_until)


def compute_artist_weights(
    artist_concerts: Dict[str, List[int]]
) -> Dict[str, float]:
    """
    Compute a raw weight for each artist.

    artist_concerts: {spotify_artist_id: [days_until_concert, ...]}
      An artist with two concerts — one in 10 days and one in 40 days —
      gets the *sum* of both weights.

    Returns: {spotify_artist_id: raw_weight}
    """
    weights: Dict[str, float] = {}
    for artist_id, days_list in artist_concerts.items():
        future = [d for d in days_list if d > 0]
        if future:
            weights[artist_id] = sum(concert_weight(d) for d in future)
    return weights


def allocate_slots(
    weights: Dict[str, float],
    target_size: int,
    min_slots: int = 2,
) -> Dict[str, int]:
    """
    Distribute `target_size` track slots across artists proportional to their weights.

    Artists whose exact proportional share is below `min_slots` are excluded —
    a far-away concert doesn't justify playlist space when near-term concerts
    already fill the target. The exponential decay in weights handles this
    naturally: a concert 90 days out has ~5% weight of one tomorrow.

    Uses Hamilton's method (largest remainder) so the total equals target_size
    exactly and no slots are wasted.

    Returns: {spotify_artist_id: num_slots}  (only artists with slots > 0)
    """
    if not weights:
        return {}

    total_weight = sum(weights.values())
    if total_weight == 0.0:
        return {}

    # Exact proportional allocation
    exact: Dict[str, float] = {
        aid: (w / total_weight) * target_size
        for aid, w in weights.items()
    }

    # Exclude artists whose share is too small to justify even min_slots
    qualified = {aid: e for aid, e in exact.items() if e >= min_slots}

    if not qualified:
        top = max(weights, key=weights.__getitem__)
        return {top: target_size}

    # Recompute proportions over qualified artists only
    q_weight_total = sum(weights[a] for a in qualified)
    exact_q: Dict[str, float] = {
        aid: (weights[aid] / q_weight_total) * target_size
        for aid in qualified
    }

    # Floor each value (enforce min_slots floor)
    floors: Dict[str, int] = {
        aid: max(min_slots, math.floor(v)) for aid, v in exact_q.items()
    }

    # Hamilton's method: distribute remaining slots by largest fractional remainder
    remainders: Dict[str, float] = {
        aid: exact_q[aid] - math.floor(exact_q[aid]) for aid in exact_q
    }
    leftover = target_size - sum(floors.values())

    for aid, _ in sorted(remainders.items(), key=lambda x: x[1], reverse=True):
        if leftover <= 0:
            break
        floors[aid] += 1
        leftover -= 1

    # If min_slots floors pushed us over budget, trim from lightest artists
    while sum(floors.values()) > target_size:
        for aid in sorted(weights, key=weights.__getitem__):
            if sum(floors.values()) <= target_size:
                break
            if aid in floors and floors[aid] > min_slots:
                floors[aid] -= 1

    logger.debug('Slot allocation:')
    for aid, slots in sorted(floors.items(), key=lambda x: -x[1]):
        logger.debug(f'  {aid}: {slots} slots (weight={weights.get(aid, 0):.3f})')

    return floors
