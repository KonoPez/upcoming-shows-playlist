"""
Inspect how names resolve, and what else they could have resolved to.

`resolution_rows` reads only the cache, so it is free to run and shows exactly
what the last pipeline run used. `find_candidates` calls Spotify (and
setlist.fm, when configured) and pages past the ten results `resolve_artist`
sees: with dozens of same-name artists, the right one is often on page two.
"""

import time
from datetime import date
from typing import Optional

import requests
import spotipy

from artist_resolver import UNRESOLVED_SENTINEL, _match_score, _normalize
from cache import Cache
from repair import pins
from sources.models import Concert, Track
from sources.setlist import BASE_URL as SETLIST_BASE_URL
from spotify_client.client import cached_artist_name, cached_artist_tracks, pad_release_date

SEARCH_PAGE_SIZE = 10      # Spotify rejects larger pages for this app
MAX_SEARCH_PAGES = 5
MAX_CANDIDATES = 12
RELEASES_SHOWN = 5


def current_resolution(cache: Cache, name: str, tm_spotify_id: str = '') -> tuple[Optional[str], str]:
    """
    (artist_id, method) for `name` as the pipeline would see it, cache only.
    method is 'pinned', 'pinned: no artist', 'ticketmaster link', 'search',
    'unresolved' or 'not yet resolved'.
    """
    pin = pins.lookup_artist(cache, name)
    if pin is not None:
        return pin.artist_id, 'pinned' if pin.artist_id else 'pinned: no artist'

    cached = cache.get(f'artist_resolve:{name.lower().strip()}')
    if cached is None:
        return None, 'not yet resolved'
    if cached == UNRESOLVED_SENTINEL:
        return None, 'unresolved'
    return cached, 'ticketmaster link' if cached == tm_spotify_id else 'search'


def latest_releases(tracks: list[Track], n: int = RELEASES_SHOWN) -> list[str]:
    """'Album (year)' for the newest `n` releases in a discography."""
    albums = {t.album_id: t for t in tracks}.values()
    newest = sorted(albums, key=lambda t: pad_release_date(t.release_date), reverse=True)
    return [f'{t.album_name} ({t.release_date[:4]})' for t in newest[:n]]


def resolution_rows(concerts: list[Concert], cache: Cache) -> list[dict]:
    """One row per billed name: the raw event title, the parsed name, and what it resolved to."""
    rows = []
    for c in sorted(concerts, key=lambda c: (c.event_date, c.event_name, c.is_opener)):
        artist_id, method = current_resolution(cache, c.artist_name, c.tm_spotify_id)
        tracks = cached_artist_tracks(artist_id, cache) if artist_id else None
        rows.append({
            'date': c.event_date.isoformat(),
            'event': c.event_name,
            'venue': c.venue.replace('\n', ', '),
            'source': c.source,
            'billed_as': c.artist_name,
            'role': 'opener' if c.is_opener else 'headliner',
            'method': method,
            'artist_id': artist_id,
            'spotify_name': cached_artist_name(artist_id, cache) if artist_id else None,
            'latest_releases': latest_releases(tracks) if tracks else None,
        })
    return rows


def _search_pages(sp: spotipy.Spotify, query: str, pages: int) -> list[dict]:
    items: list[dict] = []
    for page in range(pages):
        result = sp.search(q=query, type='artist', limit=SEARCH_PAGE_SIZE, offset=page * SEARCH_PAGE_SIZE)
        batch = result.get('artists', {}).get('items', [])
        items.extend(batch)
        if len(batch) < SEARCH_PAGE_SIZE:
            break
    return items


def rank_candidates(name: str, items: list[dict], limit: int = MAX_CANDIDATES) -> list[dict]:
    """Unique search results that match `name` at all, best match first, search order within a score."""
    norm_query = _normalize(name)
    seen: set[str] = set()
    scored = []
    for order, item in enumerate(items):
        artist_id = item.get('id')
        if not artist_id or artist_id in seen:
            continue
        seen.add(artist_id)
        score = _match_score(name, norm_query, item.get('name', ''))
        if score > 0:
            scored.append((-score, order, {'artist_id': artist_id, 'name': item['name'], 'match_score': score}))
    return [c for *_, c in sorted(scored, key=lambda s: s[:2])][:limit]


def _releases(sp: spotipy.Spotify, artist_id: str) -> list[str]:
    result = sp._get(f'artists/{artist_id}/albums?include_groups=album,single&limit={SEARCH_PAGE_SIZE}')
    albums = sorted(result.get('items', []), key=lambda a: pad_release_date(a.get('release_date', '')), reverse=True)
    return [f"{a['name']} ({a['release_date'][:4]}, {a['album_type']})" for a in albums[:RELEASES_SHOWN]]


def setlist_artists(name: str, api_key: str) -> list[dict]:
    """setlist.fm artists matching `name`, for choosing a `pin-setlist` MBID."""
    resp = requests.get(
        f'{SETLIST_BASE_URL}/search/artists',
        headers={'x-api-key': api_key, 'Accept': 'application/json'},
        params={'artistName': name, 'sort': 'relevance', 'p': 1},
        timeout=10,
    )
    if resp.status_code == 404:   # setlist.fm's answer for "no matches"
        return []
    resp.raise_for_status()
    norm = _normalize(name)
    return [
        {'mbid': a['mbid'], 'name': a['name'], 'disambiguation': a.get('disambiguation') or None}
        for a in resp.json().get('artist', [])
        if _normalize(a.get('name', '')) == norm
    ]


def find_candidates(
    name: str,
    sp: spotipy.Spotify,
    cache: Cache,
    setlist_api_key: str = '',
) -> dict:
    current_id, method = current_resolution(cache, name)
    items = _search_pages(sp, f'artist:"{name}"', MAX_SEARCH_PAGES) + _search_pages(sp, name, 1)

    candidates = rank_candidates(name, items)
    for c in candidates:
        c['current'] = c['artist_id'] == current_id
        c['your_plays'] = sum(cache.get_play_counts(c['artist_id']).values())
        try:
            c['releases'] = _releases(sp, c['artist_id'])
        except spotipy.SpotifyException as e:
            c['releases'] = f'error: {e.http_status}'
        time.sleep(0.2)

    result = {
        'name': name,
        'current': {'artist_id': current_id, 'method': method},
        'spotify': candidates,
    }
    if setlist_api_key:
        result['setlist_fm'] = setlist_artists(name, setlist_api_key)
    return result
