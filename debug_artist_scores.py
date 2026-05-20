#!/usr/bin/env python3
"""
Print every track in an artist's discography with per-signal scores.

Usage:
    python debug_artist_scores.py "Artist Name"
"""

import sys
from datetime import date

from cache import Cache
from config import config
from artist_resolver import resolve_artist
from spotify_client.auth import get_spotify_client
from spotify_client.client import SpotifyClient, deduplicate_tracks
from sources.setlist import SetlistClient
from sources.lastfm import LastFmClient
from sources.models import normalize_track_name
from playlist_logic.scoring import (
    score_track,
    _recency_score,
    _familiarity,
    _parse_release_date,
    SETLIST_W, LASTFM_W, RECENCY_W, NOVELTY_W,
)


def main(artist_name: str) -> None:
    cache = Cache()
    today = date.today()

    sp_raw = get_spotify_client(
        client_id=config.spotify_client_id,
        redirect_uri=config.spotify_redirect_uri,
        token_path=config.spotify_token_path,
        open_browser=False,
    )
    sp = SpotifyClient(sp_raw)

    print(f'Resolving "{artist_name}"...')
    artist_id = resolve_artist(artist_name, sp_raw, cache)
    if not artist_id:
        print(f'Could not resolve "{artist_name}" to a Spotify artist.')
        sys.exit(1)

    print(f'Fetching discography...')
    tracks = sp.get_artist_tracks(artist_id, cache)
    if not tracks:
        print('No tracks found.')
        sys.exit(1)

    spotify_familiarity = sp.get_user_familiarity(cache)
    play_counts = cache.get_play_counts(artist_id)

    setlist_scores = None
    if config.setlist_fm_api_key:
        setlist_scores = SetlistClient(config.setlist_fm_api_key, cache).get_setlist_scores(artist_name)

    lastfm_scores = None
    if config.lastfm_api_key:
        lastfm_scores = LastFmClient(config.lastfm_api_key, cache).get_popularity_scores(artist_name)

    tracks = deduplicate_tracks(tracks, lastfm_scores)

    # Score all tracks
    scored = []
    for t in tracks:
        total = score_track(t, spotify_familiarity, play_counts, today, setlist_scores, lastfm_scores)

        release_date = _parse_release_date(t.release_date, t.release_date_precision)
        recency = _recency_score(release_date, today)
        novelty = 1.0 - _familiarity(t.id, spotify_familiarity, play_counts)
        name_key = normalize_track_name(t.name)
        sl = setlist_scores.get(name_key, 0.0) if setlist_scores else None
        lf = lastfm_scores.get(name_key, 0.0) if lastfm_scores else None

        scored.append((total, t, sl, lf, recency, novelty))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Header
    w_sl = SETLIST_W if setlist_scores else 0.0
    w_lf = LASTFM_W  if lastfm_scores  else 0.0
    total_w = w_sl + w_lf + RECENCY_W + NOVELTY_W
    print(f'\n{artist_name} — {len(scored)} tracks  (weights: sl={w_sl/total_w:.0%} lf={w_lf/total_w:.0%} rec={RECENCY_W/total_w:.0%} nov={NOVELTY_W/total_w:.0%})\n')
    print(f'  {"Score":>5}  {"SL":>5}  {"LF":>5}  {"Rec":>5}  {"Nov":>5}   {"Track":<40}  Album')
    print(f'  {"─"*5}  {"─"*5}  {"─"*5}  {"─"*5}  {"─"*5}   {"─"*40}  {"─"*30}')

    for total, t, sl, lf, recency, novelty in scored:
        sl_str  = f'{sl:.0%}'  if sl  is not None else '  —  '
        lf_str  = f'{lf:.0%}'  if lf  is not None else '  —  '
        release = (t.release_date or '?')[:4]
        print(
            f'  {total:>4.0%}  {sl_str:>5}  {lf_str:>5}  {recency:>4.0%}  {novelty:>4.0%}'
            f'   {t.name:<40}  {t.album_name} ({release})'
        )


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python debug_artist_scores.py "Artist Name"')
        sys.exit(1)
    main(' '.join(sys.argv[1:]))
