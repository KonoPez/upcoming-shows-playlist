"""
Find setlist.fm and Last.fm song names that never reach a Spotify track.

Scores are looked up by `normalize_track_name(track.name)`, so any stored
name that normalises differently is silently worth nothing. Each such miss
so far (curly apostrophes, live suffixes on the stored side, an EP-version
pressing) was found by hand-written scripts against the cache; this is that
script. Cache-only: run `--dry-run` first so the artist's data is cached.

An unmatched name is classified by what would have caught it:
- `folds_to`: equal to a track after folding case, quotes and punctuation —
  a normalisation bug.
- `close_to`: a near spelling of a track — possibly a typo, possibly
  a different song.
- neither: a song not in the discography (unreleased, a cover, or a
  namesake's song mixed in by Last.fm).
"""

import difflib
import re
from collections import defaultdict

from sources.models import Track
from track_names import normalize_track_name

CLOSE_MATCH_CUTOFF = 0.85


def fold(name: str) -> str:
    """Aggressive comparison key: quotes unified, punctuation and spacing dropped."""
    name = normalize_track_name(name).replace('’', "'").replace('‘', "'")
    name = re.sub(r"[^\w']+", ' ', name)
    return re.sub(r'\s+', ' ', name).strip()


def _base_title(name: str) -> str:
    """Title with every parenthetical and dash suffix removed, for spotting duplicates."""
    name = re.sub(r'\s*[\(\[].*?[\)\]]', '', name)
    name = re.sub(r'\s+[-–]\s+.*$', '', name)
    return fold(name)


def unmatched_names(scores: dict[str, float], tracks: list[Track]) -> list[dict]:
    keys = {normalize_track_name(t.name) for t in tracks}
    by_fold = {fold(t.name): t.name for t in tracks}
    folded_titles = list(by_fold)

    misses = []
    for stored, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        # The pipeline compares the stored name as-is (issue #5), so must this.
        if stored in keys:
            continue
        entry = {'stored_as': stored, 'score': round(score, 3)}
        folded = fold(stored)
        if folded in by_fold:
            entry['folds_to'] = by_fold[folded]
        else:
            close = difflib.get_close_matches(folded, folded_titles, n=1, cutoff=CLOSE_MATCH_CUTOFF)
            if close:
                entry['close_to'] = by_fold[close[0]]
        misses.append(entry)
    return misses


def possible_duplicates(tracks: list[Track]) -> list[list[str]]:
    """Titles sharing a base title but not a lookup key, so dedup never compares them."""
    groups: dict[str, set[str]] = defaultdict(set)
    names: dict[str, set[str]] = defaultdict(set)
    for t in tracks:
        base = _base_title(t.name)
        groups[base].add(normalize_track_name(t.name))
        names[base].add(t.name)
    return [sorted(names[b]) for b, keys in sorted(groups.items()) if len(keys) > 1]


def audit_artist(
    tracks: list[Track],
    setlist_scores: dict[str, float],
    lastfm_scores: dict[str, float],
) -> dict:
    keys_with_signal = {*setlist_scores, *lastfm_scores}
    return {
        'tracks': len(tracks),
        'setlist_unmatched': unmatched_names(setlist_scores, tracks),
        'lastfm_unmatched': unmatched_names(lastfm_scores, tracks),
        'tracks_without_signal': sorted(
            t.name for t in tracks if normalize_track_name(t.name) not in keys_with_signal
        ),
        'possible_duplicates': possible_duplicates(tracks),
    }
