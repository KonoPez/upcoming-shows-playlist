"""
Drop one artist's cached data so the next run refetches it.

`--clear-cache discography` refetches every artist, at ~27 Spotify requests
each, and a burst like that once drew a 23-hour 429. This touches one.

The resolution itself is kept: re-searching could swap a correct artist for a
namesake. Only a cached failure is dropped, since retrying it is harmless.
"""

from artist_resolver import UNRESOLVED_SENTINEL
from cache import Cache
from repair.lookup import current_resolution
from spotify_client.client import _artist_name_key, _artist_tracks_key, cached_artist_name

NAME_KEYED_PREFIXES = ('setlist:', 'lastfm:', 'lastfm_listeners:', 'lastfm_similar:')


def refresh_artist(cache: Cache, name: str) -> dict:
    artist_id, method = current_resolution(cache, name)
    names = {name.lower().strip()}
    removed: list[str] = []

    def drop(key: str) -> None:
        if cache.get(key) is not None:
            cache.delete(key)
            removed.append(key)

    if artist_id:
        canonical = cached_artist_name(artist_id, cache)
        if canonical:
            names.add(canonical.lower().strip())
        drop(_artist_tracks_key(artist_id))
        drop(_artist_name_key(artist_id))
    elif method == 'unresolved':
        drop(f'artist_resolve:{name.lower().strip()}')

    for n in sorted(names):
        for prefix in NAME_KEYED_PREFIXES:
            drop(prefix + n)
        for key in cache.get_prefix(f'tm_openers:{n}:'):
            drop(key)

    return {'name': name, 'artist_id': artist_id, 'method': method, 'removed': removed}
