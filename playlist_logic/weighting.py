"""
Concert-proximity weighting and track-slot allocation.

Weights follow exponential decay with a 21-day half-life:
  - Concert today     → weight = 1.00
  - Concert tomorrow  → weight ≈ 0.968
  - Concert in 3 wks  → weight ≈ 0.50
  - Concert in 6 wks  → weight ≈ 0.25
  - Concert in 90 days → weight ≈ 0.056

Artist familiarity then trims that weight slightly: an artist you already know
well needs less prep than one you have never heard, so their share is scaled by
`novelty_multiplier`. The trim is deliberately small — it separates artists at
equal proximity (a festival bill) and decides who survives the minimum-budget
floor on a crowded calendar, but it never outranks proximity itself.

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
from typing import Optional

from sources.models import Concert

logger = logging.getLogger(__name__)

HALF_LIFE_DAYS = 21.0
_LAMBDA = math.log(2) / HALF_LIFE_DAYS
HEADLINER_BONUS = 1.5  
MIN_ARTIST_BUDGET_MS = 120_000
FAMILIARITY_PENALTY = 0.33   # a fully familiar artist keeps 67% of their proximity weight,
                             # a swing kept just under HEADLINER_BONUS so billing stays decisive
TOP_SCORE_W = 0.60           # Spotify's ranking: every device, whole window
PLAY_LOG_W  = 0.40           # local play log: finer grained, but only what cron sampled


def concert_weight(days_until: int) -> float:
    """
    Exponential weight for a concert that is `days_until` days away.
    A concert today (days_until == 0) receives the peak weight, 1.0.
    Returns 0.0 for concerts in the past (days_until < 0).
    """
    if days_until < 0:
        return 0.0
    return math.exp(-_LAMBDA * days_until)


def novelty_multiplier(familiarity: float) -> float:
    """
    Weight multiplier for an artist you already know: 1.0 when unfamiliar,
    1 - FAMILIARITY_PENALTY at maximum familiarity.

    The input is clamped so a caller passing a stray out-of-range score cannot
    invert the weight or push it past the intended floor.
    """
    return 1.0 - FAMILIARITY_PENALTY * min(max(familiarity, 0.0), 1.0)


def compute_artist_familiarity_scores(
    candidate_ids: list[str],
    top_scores: dict[str, float],
    play_counts: dict[str, int],
    normalize_against: Optional[list[str]] = None,
) -> dict[str, float]:
    """
    Return {artist_id: familiarity_score} for each candidate.

    Two signals, blended by TOP_SCORE_W / PLAY_LOG_W and normalised by the
    weights actually present — the same shape as `score_artist_enjoyment` and
    `score_track`:

      in a top-artist list:  (TOP_SCORE_W * top_score + PLAY_LOG_W * play_score)
                             / (TOP_SCORE_W + PLAY_LOG_W)
      otherwise:             play_score
      empty play log:        top_score alone, or 0.0 if neither signal has one

    An artist missing from `top_scores` is treated as *no opinion*, not as a
    zero. Spotify truncates each window at fifty, so absence there means the
    artist fell off the end of a list, which is not the same claim as "never
    played" — folding it in as a zero would drag down every artist the local
    log knows well but the top fifty happens to exclude. This is also what
    keeps the discovery pool unaffected: almost none of those candidates appear
    in a top list, so their score stays exactly the play-history term it was.

    Spotify's ranking carries the heavier weight because it is the
    better-founded of the two — it sees every device across the whole window,
    while the play log only sees what polling recently-played on cron runs
    happened to catch. But the log carries the finer resolution, which is why
    it must not be dropped: this used to be `max(top_score, play_score)`, and a
    flat top-tier score meant an artist played twice and one played two hundred
    times both came back at exactly 1.0. On a concert bill, where most acts sit
    somewhere inside the short-term fifty, that collapsed the signal to a
    constant and the play counts separating them were thrown away.

    `normalize_against` names the artists whose play counts set the
    log-normalisation ceiling. It defaults to `candidate_ids` — the relative
    mode discovery wants, where an artist with 3 plays scores meaningfully if
    every other candidate also has few or zero plays, since normalising a pool
    of unknowns against the user's most-listened-to artist overall would flatten
    them all to zero.

    The prep playlist passes the whole play history instead, making the score
    absolute. Its candidate set is one concert bill, sometimes two artists, and
    under the relative mode whichever of them had more plays would score 1.0
    even on two lifetime listens — and adding a heavily-played artist to the
    calendar would silently lower everyone else's score.
    """
    reference_ids = normalize_against if normalize_against is not None else candidate_ids
    max_count = max((play_counts.get(aid, 0) for aid in reference_ids), default=0)
    # An empty log is not a log full of zeroes. Before the first few `--update`
    # runs accumulate history there is nothing to normalise against and no
    # artist can be told apart, so the term drops out rather than dragging
    # every score down toward its own uninformative zero.
    play_log_has_data = max_count > 0
    log_max = math.log(max_count + 1) if play_log_has_data else 1

    familiarity: dict[str, float] = {}
    for aid in candidate_ids:
        score   = 0.0
        total_w = 0.0

        if play_log_has_data:
            score   += PLAY_LOG_W * (math.log(play_counts.get(aid, 0) + 1) / log_max)
            total_w += PLAY_LOG_W

        if aid in top_scores:
            score   += TOP_SCORE_W * top_scores[aid]
            total_w += TOP_SCORE_W

        familiarity[aid] = score / total_w if total_w else 0.0

    return familiarity


def compute_artist_weights(
    artist_concerts: dict[str, list[Concert]],
    today: date,
    familiarity: Optional[dict[str, float]] = None,
) -> dict[str, float]:
    """
    Compute a raw weight for each artist.

    artist_concerts: {spotify_artist_id: [Concert, ...]}
      Each entry is one concert appearance. An artist with two concerts
      gets the *sum* of both weights. Headliner slots (is_opener=False) 
      receive a HEADLINER_BONUS multiplier

    familiarity: {spotify_artist_id: familiarity_score} — optional. When given,
      each artist's summed weight is scaled by `novelty_multiplier`, shifting
      prep time toward artists the user does not already know. The factor is
      constant per artist, so scaling the sum is equivalent to scaling each
      concert. Artists missing from the dict are treated as unfamiliar.

    Returns: {spotify_artist_id: raw_weight}
    """
    weights: dict[str, float] = defaultdict(float)

    for artist_id, concerts in artist_concerts.items():
        for c in concerts:
            w = concert_weight(c.days_until(today)) * (1 if c.is_opener else HEADLINER_BONUS)
            weights[artist_id] += w

    if familiarity:
        for artist_id in weights:
            weights[artist_id] *= novelty_multiplier(familiarity.get(artist_id, 0.0))

    return {aid: w for aid, w in weights.items() if w > 0}


def _proportional_split(
    weights: dict[str, float],
    target_duration_ms: int,
) -> dict[str, int]:
    """
    Split `target_duration_ms` across artists in proportion to their weights.
    Hamilton's method (largest remainder) distributes the rounding so the
    budgets sum to the target exactly.
    """
    if not weights:
        return {}

    total_weight = sum(weights.values())
    if total_weight == 0.0:
        return {}

    exact = {aid: (w / total_weight) * target_duration_ms for aid, w in weights.items()}
    floors = {aid: math.floor(v) for aid, v in exact.items()}

    remainders = {aid: exact[aid] - floors[aid] for aid in exact}
    leftover = target_duration_ms - sum(floors.values())

    for aid, _ in sorted(remainders.items(), key=lambda x: x[1], reverse=True):
        if leftover <= 0:
            break
        floors[aid] += 1
        leftover -= 1

    return floors


def allocate_slots(
    weights: dict[str, float],
    target_duration_ms: int,
    min_budget_ms: int = 0,
) -> dict[str, int]:
    """
    Distribute `target_duration_ms` across artists proportional to their weights.
    Every artist with a non-zero weight receives a budget.

    `min_budget_ms` imposes a floor: while any artist holds less than it, the one
    with the *lowest* allocation is evicted and the target re-split across the
    survivors, handing the freed time back in proportion to their weights.
    The floor is off by default because discovery depends on its absence: it
    guarantees a slot to every artist it selects.

    Returns: {spotify_artist_id: duration_budget_ms} for the survivors.
    """
    slots = _proportional_split(weights, target_duration_ms)

    while min_budget_ms > 0 and slots:
        smallest = min(slots, key=lambda aid: (slots[aid], aid))
        if slots[smallest] >= min_budget_ms:
            break
        
        if len(slots) == 1:
            break

        del slots[smallest]
        slots = _proportional_split(
            {aid: weights[aid] for aid in slots}, target_duration_ms
        )

    logger.debug('Duration budget allocation:')
    for aid, budget in sorted(slots.items(), key=lambda x: -x[1]):
        logger.debug(f'  {aid}: {budget // 60_000}m budget (weight={weights.get(aid, 0):.3f})')

    return slots
