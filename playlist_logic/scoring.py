"""
Track scoring for playlist inclusion.

Each track receives a composite score from three signals:
  - Popularity (55%): Spotify's popularity metric — the dominant signal, as a
    proxy for "fan favorites and tracks likely to appear in a setlist." Weighted
    highest so that catalog hits (e.g. 218M-stream tracks from 2023) rank above
    obscure tracks from a brand-new album.
  - Recency (25%): Tracks from the most recent 18 months score highest. New
    material is likely to be featured on tour.
  - Novelty (20%): The inverse of familiarity. Tracks the user hasn't heard
    (or rarely hears) score highest, pushing the playlist toward discovery.

Familiarity is computed from two sources, combined by taking the max:
  - Spotify API: top tracks (short/medium/long term) + recently played.
  - Local SQLite history: accumulated play counts from every weekly run.
    Uses a soft cap — 10+ plays = fully familiar (score 1.0).
"""

import logging
import math
from datetime import date
from typing import Dict, List

logger = logging.getLogger(__name__)

POPULARITY_W = 0.55
RECENCY_W = 0.25
NOVELTY_W = 0.20

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
    spotify_familiarity: Dict[str, float],
    play_counts: Dict[str, int],
) -> float:
    """Combined familiarity from Spotify API signal and local play history."""
    api_score = spotify_familiarity.get(track_id, 0.0)
    history_score = min(play_counts.get(track_id, 0) / FAMILIAR_AT_N_PLAYS, 1.0)
    return max(api_score, history_score)


def score_track(
    track: dict,
    spotify_familiarity: Dict[str, float],
    play_counts: Dict[str, int],
    today: date,
) -> float:
    """Return a composite score (0.0–1.0) for a single track."""
    popularity = track.get('popularity', 0) / 100.0

    release_date = _parse_release_date(
        track.get('release_date', '2000-01-01'),
        track.get('release_date_precision', 'year'),
    )
    recency = _recency_score(release_date, today)

    fam = _familiarity(track.get('id', ''), spotify_familiarity, play_counts)
    novelty = 1.0 - fam

    return POPULARITY_W * popularity + RECENCY_W * recency + NOVELTY_W * novelty


def _interleave_albums(tracks: List[dict]) -> List[dict]:
    """
    Reorder tracks so no two consecutive tracks share the same album.
    Album groups are sorted newest-first by release date, then round-robined.
    Returns tracks unchanged if they all belong to a single album.
    """
    if not tracks:
        return tracks

    by_album: dict = {}
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
    tracks: List[dict],
    num_slots: int,
    spotify_familiarity: Dict[str, float],
    play_counts: Dict[str, int],
    today: date,
    max_per_album: int = 6,
) -> List[dict]:
    """
    Score all tracks for an artist and return the top `num_slots`.

    Applies a per-album cap during greedy selection (max_per_album, default 4)
    so a single album — especially a just-released one where all tracks score
    equally on popularity — cannot claim every slot. Tracks without an album_id
    are uncapped (legacy/test data).

    The selected tracks are then interleaved across albums (newest album first,
    round-robin) so no two consecutive tracks come from the same album.
    """
    if not tracks or num_slots == 0:
        return []

    scored = [
        (score_track(t, spotify_familiarity, play_counts, today), t)
        for t in tracks
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Greedy selection with per-album cap
    album_counts: dict = {}
    selected = []
    for _, t in scored:
        if len(selected) >= num_slots:
            break
        aid = t.get('album_id') or ''
        if not aid or album_counts.get(aid, 0) < max_per_album:
            selected.append(t)
            if aid:
                album_counts[aid] = album_counts.get(aid, 0) + 1

    return _interleave_albums(selected)
