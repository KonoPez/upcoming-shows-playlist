"""
Track scoring for playlist inclusion.

Weights depend on whether setlist.fm data is available for the artist:

  WITH setlist data (SETLIST_FM_API_KEY configured):
    - Setlist frequency (60%): how often the artist plays this song live.
      Tracks played at every sampled show score 1.0; unplayed tracks score 0.0.
      This is the dominant signal — actual live evidence beats any proxy.
    - Recency (20%): tracks from the most recent 18 months score highest.
      Kept at a reduced weight because new material often appears in setlists
      quickly, but older staples should not be penalised too heavily.
    - Novelty (20%): inverse of familiarity — surfaces tracks the user hasn't
      heard yet.

  WITHOUT setlist data:
    - Recency (80%): the dominant proxy for "will they play this on tour."
    - Novelty (20%): same as above.

Familiarity is computed from two sources, combined by taking the max:
  - Spotify API: top tracks (short/medium/long term) + recently played.
  - Local SQLite history: accumulated play counts from every weekly run.
    Uses a soft cap — 10+ plays = fully familiar (score 1.0).
"""

import logging
import math
from datetime import date
from typing import Optional  # noqa: F401 — used in Optional[dict] annotations

logger = logging.getLogger(__name__)

NOVELTY_W = 0.20

# Weights when setlist.fm data is available for this artist
SETLIST_W = 0.60
RECENCY_W = 0.20

# Weights when no setlist data is available (recency is the best proxy)
RECENCY_W_FALLBACK = 0.80

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
    spotify_familiarity: dict[str, float],
    play_counts: dict[str, int],
) -> float:
    """Combined familiarity from Spotify API signal and local play history."""
    api_score = spotify_familiarity.get(track_id, 0.0)
    history_score = min(play_counts.get(track_id, 0) / FAMILIAR_AT_N_PLAYS, 1.0)
    return max(api_score, history_score)


def score_track(
    track: dict,
    spotify_familiarity: dict,
    play_counts: dict,
    today: date,
    setlist_scores: Optional[dict] = None,
) -> float:
    """
    Return a composite score for a single track (0.0–1.0).

    With setlist data:    60% setlist frequency + 20% recency + 20% novelty.
    Without setlist data: 80% recency + 20% novelty.

    The higher recency weight without setlist data reflects that recency is the
    best available proxy for "likely to be played live" when actual data is absent.
    With real setlist data, recency is reduced so older staples aren't unfairly
    penalised relative to new tracks.
    """
    release_date = _parse_release_date(
        track.get('release_date', '2000-01-01'),
        track.get('release_date_precision', 'year'),
    )
    recency = _recency_score(release_date, today)

    fam = _familiarity(track.get('id', ''), spotify_familiarity, play_counts)
    novelty = 1.0 - fam

    if setlist_scores:
        name_key = track.get('name', '').lower().strip()
        freq = setlist_scores.get(name_key, 0.0)
        return SETLIST_W * freq + RECENCY_W * recency + NOVELTY_W * novelty

    return RECENCY_W_FALLBACK * recency + NOVELTY_W * novelty


def _interleave_albums(tracks: list[dict]) -> list[dict]:
    """
    Reorder tracks so no two consecutive tracks share the same album.
    Album groups are sorted newest-first by release date, then round-robined.
    Returns tracks unchanged if they all belong to a single album.
    """
    if not tracks:
        return tracks

    by_album = {}
    for t in tracks:
        aid = t.get('album_id') or ''
        by_album.setdefault(aid, []).append(t)

    if len(by_album) <= 1:
        return tracks

    # Sort album groups newest-first so the interleaved order leads with new material
    def _album_date(items: list) -> str:
        return max(t.get('release_date', '') for t in items)

    groups = sorted(by_album.values(), key=_album_date, reverse=True)

    result = []
    while any(groups):
        for g in groups:
            if g:
                result.append(g.pop(0))
    return result


def select_tracks_for_artist(
    tracks: list,
    duration_budget_ms: int,
    spotify_familiarity: dict,
    play_counts: dict,
    today: date,
    max_per_album: int = 6,
    setlist_scores: Optional[dict] = None,
) -> list:
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
        (score_track(t, spotify_familiarity, play_counts, today, setlist_scores), t)
        for t in tracks
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Greedy selection with per-album cap, stopping when duration budget is reached
    album_counts: dict = {}
    selected = []
    total_ms = 0
    for _, t in scored:
        if total_ms >= duration_budget_ms:
            break
        aid = t.get('album_id') or ''
        if not aid or album_counts.get(aid, 0) < max_per_album:
            selected.append(t)
            total_ms += t.get('duration_ms', 0)
            if aid:
                album_counts[aid] = album_counts.get(aid, 0) + 1

    return _interleave_albums(selected)
