"""
Artist scoring and slot allocation for the discovery playlist.

Unlike the prep playlist (where every calendar artist is included), the
discovery pipeline must decide *which* artists to include based on how
much the user is likely to enjoy them.

Enjoyment score formula:
  score = FAMILIARITY_W * personal_familiarity
        + POPULARITY_W  * global_popularity   (Last.fm listener count)

When Last.fm is not configured, familiarity carries full weight.

Slot allocation weight per artist:
  weight = enjoyment_score^ENJOYMENT_EXPONENT * proximity_decay

The exponent means the top artists claim a disproportionately larger
share of the playlist than the tail — intentionally steeper than the
prep playlist, since there's no sunk cost from having bought tickets.
"""

import logging
import math
from datetime import date
from typing import Optional

from playlist_logic.weighting import concert_weight
from sources.models import Artist

logger = logging.getLogger(__name__)

FAMILIARITY_W       = 0.75
POPULARITY_W        = 0.25
SIMILARITY_W        = 0.35
ENJOYMENT_EXPONENT  = 1.5   # steeper decay toward low-scoring artists
PROXIMITY_EXPONENT  = 1.0   # full decay — concerts happening sooner get proportionally
                             # more weight. Uses concert_weight's 21-day half-life directly.


def compute_artist_familiarity_scores(
    candidate_ids: list[str],
    top_scores: dict[str, float],
    play_counts: dict[str, int],
) -> dict[str, float]:
    """
    Return {artist_id: familiarity_score} for each candidate.

    personal_familiarity = max(spotify_top_score, play_history_score)

    play_history_score is log-normalized within the candidate set so that
    an artist with 3 plays scores meaningfully if every other candidate also
    has few or zero plays. Normalization against the global maximum would
    unfairly penalize artists with modest play counts relative to the user's
    most-listened-to artist overall.
    """
    # Log-normalize play counts within the candidate set
    candidate_counts = [play_counts.get(aid, 0) for aid in candidate_ids]
    max_count = max(candidate_counts) if candidate_counts else 0

    def _play_score(count: int) -> float:
        if max_count == 0 or count == 0:
            return 0.0
        return math.log(count + 1) / math.log(max_count + 1)

    familiarity: dict[str, float] = {}
    for aid in candidate_ids:
        api_score  = top_scores.get(aid, 0.0)
        play_score = _play_score(play_counts.get(aid, 0))
        familiarity[aid] = max(api_score, play_score)

    return familiarity


def _normalize_popularity(raw_listeners: dict[str, int]) -> dict[str, float]:
    """
    Log-normalize Last.fm listener counts across the candidate set.
    Returns {} if no data is available.
    """
    if not raw_listeners:
        return {}
    max_listeners = max(raw_listeners.values())
    if max_listeners == 0:
        return {}
    log_max = math.log(max_listeners + 1)
    return {
        aid: math.log(count + 1) / log_max
        for aid, count in raw_listeners.items()
    }


def score_artist_enjoyment(
    personal_familiarity: float,
    global_popularity: Optional[float],
    similarity: Optional[float] = None,
) -> float:
    """
    Compute blended enjoyment score for a single artist.

    Signals are weighted by FAMILIARITY_W / POPULARITY_W / SIMILARITY_W and
    normalised by the sum of weights that are actually present, so the relative
    importance of available signals is preserved when some are absent.
    """
    total_w = FAMILIARITY_W
    score   = FAMILIARITY_W * personal_familiarity

    if global_popularity is not None:
        total_w += POPULARITY_W
        score   += POPULARITY_W * global_popularity

    if similarity is not None:
        total_w += SIMILARITY_W
        score   += SIMILARITY_W * similarity

    return score / total_w


def compute_artist_similarity_scores(
    candidate_names: list,
    taste_profile: dict,
    lastfm_client: object,
) -> dict[str, float]:
    """
    Return {artist_name_lower: similarity_score} for each candidate.

    For each candidate, fetches their similar artists from Last.fm and
    computes a raw score as the dot product of Last.fm similarity weights
    and the user's familiarity with those similar artists:

        raw = sum(lastfm_weight * taste_profile.get(similar_name, 0))

    Scores are log-normalised across the candidate set so a modest raw
    score still registers meaningfully when all candidates are unfamiliar.

    taste_profile keys must already be lowercased; Last.fm names are
    lowercased before lookup.
    """
    # Query Last.fm with the original-case name, but key the result lowercased
    # so callers can look up by artist_name.lower() (see the module docstring).
    raw: dict[str, float] = {}
    for name in candidate_names:
        similar = lastfm_client.get_similar_artists(name)
        raw[name.lower()] = sum(
            weight * taste_profile.get(similar_name, 0.0)
            for similar_name, weight in similar.items()
        )

    max_raw = max(raw.values()) if raw else 0.0
    if max_raw == 0.0:
        return {name.lower(): 0.0 for name in candidate_names}

    log_max = math.log(max_raw + 1)
    return {
        name: math.log(score + 1) / log_max
        for name, score in raw.items()
    }


def select_discovery_artists(
    candidate_ids: list[str],
    familiarity_scores: dict[str, float],
    raw_listeners: dict[str, int],
    max_artists: int,
    min_score: float,
    similarity_scores: Optional[dict] = None,
) -> tuple[list[str], dict[str, float]]:
    """
    Score all candidates, apply the minimum floor, cap at max_artists.

    Returns (selected_ids, enjoyment_scores) where selected_ids is ordered
    by enjoyment score descending.

    similarity_scores maps artist_name_lower → normalised similarity (0–1).
    Pass None (or omit) when Last.fm is not configured — the signal is
    dropped and the other weights normalise to fill the gap.
    """
    popularity_scores = _normalize_popularity(raw_listeners)

    enjoyment: dict[str, float] = {}
    for aid in candidate_ids:
        fam  = familiarity_scores.get(aid, 0.0)
        pop  = popularity_scores.get(aid)        # None when Last.fm not configured
        sim  = None
        if similarity_scores is not None:
            sim = similarity_scores.get(aid)  # keyed by artist ID; caller converts from names
        enjoyment[aid] = score_artist_enjoyment(fam, pop, sim)

    qualified = [
        (aid, score) for aid, score in enjoyment.items()
        if score >= min_score
    ]
    qualified.sort(key=lambda x: x[1], reverse=True)
    selected = qualified[:max_artists]

    selected_ids    = [aid for aid, _ in selected]
    selected_scores = {aid: score for aid, score in selected}

    logger.info(
        f'Discovery: {len(candidate_ids)} candidates → '
        f'{len(qualified)} above floor → {len(selected_ids)} selected'
    )
    for aid, score in selected:
        logger.debug(f'  {aid}: enjoyment={score:.3f}')

    return selected_ids, selected_scores


def compute_discovery_weights(
    artists: dict[str, Artist],
    enjoyment_scores: dict[str, float],
    today: date,
) -> dict[str, float]:
    """
    Compute slot-allocation weights combining enjoyment and concert proximity.

    weight = enjoyment^ENJOYMENT_EXPONENT * proximity_decay

    Uses the nearest upcoming concert for each artist's proximity score.
    """
    weights: dict[str, float] = {}
    for artist_id, artist in artists.items():
        enjoyment  = enjoyment_scores.get(artist_id, 0.0)
        nearest    = min(c.days_until(today) for c in artist.concerts)
        proximity  = concert_weight(nearest) ** PROXIMITY_EXPONENT
        weights[artist_id] = (enjoyment ** ENJOYMENT_EXPONENT) * proximity

    return weights
