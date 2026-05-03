"""
Track scoring for playlist inclusion.

All four signals are given fixed base weights; the score is normalised by
the sum of weights for signals that are actually available. This means the
relative importance of each signal is preserved regardless of which external
APIs are configured, and no separate fallback weight sets are needed.

  SETLIST (0.45): how often the artist plays this track live (setlist.fm).
    Frequency = appearances / shows sampled. Old staples that are played at
    every show compete fairly against new material.

  LASTFM  (0.25): global popularity from Last.fm play counts. Log-normalised
    so a track with half the plays of the #1 track scores ~0.85, not 0.5.

  RECENCY (0.15): linear decay from 1.0 at release to 0.0 at 18 months.
    New tour material typically enters setlists quickly, so recency remains
    a useful secondary signal even when live data is available.

  NOVELTY (0.15): inverse of familiarity — surfaces tracks the user hasn't
    heard yet. Familiarity combines Spotify top-tracks API signal and local
    play-count history, taking the max.

Example fallback ratios (neither Last.fm nor setlist configured):
  active weights: RECENCY + NOVELTY = 0.30 → each normalises to 0.5 / 0.5.
"""

import logging
from datetime import date
from typing import Optional

from sources.models import Track

logger = logging.getLogger(__name__)

LASTFM_W  = 0.25
SETLIST_W = 0.45
RECENCY_W = 0.15
NOVELTY_W = 0.15

RECENCY_WINDOW_DAYS = 548   # 18 months
FAMILIAR_AT_N_PLAYS = 10    # play count that maxes out familiarity


def _parse_release_date(release_date: str, precision: str) -> date:
    try:
        if len(release_date) == 10:
            return date.fromisoformat(release_date)
        if len(release_date) == 7:
            return date.fromisoformat(release_date + '-01')
        return date.fromisoformat(release_date[:4] + '-01-01')
    except (ValueError, IndexError):
        return date(2000, 1, 1)


def _recency_score(release_date: date, today: date) -> float:
    """Linear decay from 1.0 at release to 0.0 at RECENCY_WINDOW_DAYS."""
    days = (today - release_date).days
    if days <= 0:
        return 1.0
    if days >= RECENCY_WINDOW_DAYS:
        return 0.0
    return 1.0 - (days / RECENCY_WINDOW_DAYS)


def _familiarity(
    track_id: str,
    spotify_familiarity: "dict[str, float]",
    play_counts: "dict[str, int]",
) -> float:
    """Combined familiarity from Spotify API signal and local play history."""
    api_score = spotify_familiarity.get(track_id, 0.0)
    history_score = min(play_counts.get(track_id, 0) / FAMILIAR_AT_N_PLAYS, 1.0)
    return max(api_score, history_score)


def score_track(
    track: Track,
    spotify_familiarity: dict,
    play_counts: dict,
    today: date,
    setlist_scores: Optional[dict] = None,
    lastfm_scores: Optional[dict] = None,
) -> float:
    """
    Return a composite score for a single track (0.0–1.0).

    Weights are normalised by the sum of weights for available signals, so
    the relative importance of each signal is preserved regardless of which
    APIs are configured. Recency and novelty are always active; setlist and
    Last.fm weights only enter the denominator when their data is present.
    """
    release_date = _parse_release_date(track.release_date, track.release_date_precision)
    recency = _recency_score(release_date, today)
    novelty = 1.0 - _familiarity(track.id, spotify_familiarity, play_counts)

    name_key = track.name.lower().strip()
    freq       = setlist_scores.get(name_key, 0.0) if setlist_scores else 0.0
    popularity = lastfm_scores.get(name_key, 0.0)  if lastfm_scores  else 0.0

    w_setlist = SETLIST_W if setlist_scores else 0.0
    w_lastfm  = LASTFM_W  if lastfm_scores  else 0.0
    total_w   = w_setlist + w_lastfm + RECENCY_W + NOVELTY_W

    return (
        w_setlist * freq
        + w_lastfm  * popularity
        + RECENCY_W * recency
        + NOVELTY_W * novelty
    ) / total_w


def _interleave_albums(tracks: "list[Track]") -> "list[Track]":
    """
    Reorder tracks so no two consecutive tracks share the same album.
    Album groups are sorted newest-first by release date, then round-robined.
    Returns tracks unchanged if they all belong to a single album.
    """
    if not tracks:
        return tracks

    by_album: dict[str, list[Track]] = {}
    for t in tracks:
        by_album.setdefault(t.album_id, []).append(t)

    if len(by_album) <= 1:
        return tracks

    # Sort album groups newest-first so the interleaved order leads with new material
    def _album_date(items: "list[Track]") -> str:
        return max(t.release_date for t in items)

    groups = sorted(by_album.values(), key=_album_date, reverse=True)

    result = []
    while any(groups):
        for g in groups:
            if g:
                result.append(g.pop(0))
    return result


def select_tracks_for_artist(
    tracks: "list[Track]",
    duration_budget_ms: int,
    spotify_familiarity: dict,
    play_counts: dict,
    today: date,
    max_per_album: int = 6,
    setlist_scores: Optional[dict] = None,
    lastfm_scores: Optional[dict] = None,
) -> "list[Track]":
    """
    Score all tracks for an artist and greedily select them until the
    cumulative duration reaches `duration_budget_ms`.

    Applies a per-album cap (max_per_album) so a single album cannot claim
    every slot. Tracks without an album_id are uncapped.

    The selected tracks are interleaved across albums (newest album first,
    round-robin) so no two consecutive tracks come from the same album.
    """
    if not tracks or duration_budget_ms == 0:
        return []

    scored = [
        (score_track(t, spotify_familiarity, play_counts, today, setlist_scores, lastfm_scores), t)
        for t in tracks
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Greedy selection with per-album cap, stopping when duration budget is reached
    album_counts: dict[str, int] = {}
    selected: list[Track] = []
    total_ms = 0
    for _, t in scored:
        if total_ms >= duration_budget_ms:
            break
        if not t.album_id or album_counts.get(t.album_id, 0) < max_per_album:
            selected.append(t)
            total_ms += t.duration_ms
            if t.album_id:
                album_counts[t.album_id] = album_counts.get(t.album_id, 0) + 1

    return _interleave_albums(selected)
