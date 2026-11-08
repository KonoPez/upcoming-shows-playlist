"""
python -m repair resolutions                     how every upcoming billed name resolved
python -m repair candidates NAME                 every Spotify / setlist.fm artist NAME could be
python -m repair pin NAME SPOTIFY_ID             force NAME to an artist
python -m repair pin NAME --none [--until DATE]  force NAME to no artist
python -m repair unpin NAME
python -m repair pin-setlist NAME MBID           force the setlist.fm artist for NAME's Spotify artist
python -m repair unpin-setlist NAME
python -m repair pins                            list every pin
python -m repair refresh NAME                    drop NAME's cached discography / setlist / Last.fm / lineups
python -m repair audit NAME                      setlist.fm / Last.fm names that match no Spotify track

Add --json to any command for machine-readable output.
"""

import argparse
import json
import sys
from datetime import date

from cache import Cache
from config import config
from repair import pins
from repair.audit import audit_artist
from repair.lookup import current_resolution, find_candidates, resolution_rows
from repair.refresh import refresh_artist
from spotify_client.client import cached_artist_name, cached_artist_tracks


def _spotify():
    from spotify_client.auth import get_spotify_client
    return get_spotify_client(
        client_id=config.spotify_client_id,
        redirect_uri=config.spotify_redirect_uri,
        token_path=config.spotify_token_path,
        open_browser=False,
    )


def _emit(data, as_json: bool, text) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        text(data)


def _require_artist(cache: Cache, name: str) -> str:
    artist_id, method = current_resolution(cache, name)
    if not artist_id:
        sys.exit(f'"{name}" has no Spotify artist ({method}). Pin one first, or run --dry-run.')
    return artist_id


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_resolutions(cache: Cache, args) -> None:
    import main
    from sources.ticketmaster import TicketmasterClient, enrich_with_openers

    concerts = main.fetch_all_concerts(config.concert_window_days, cache)
    if config.ticketmaster_api_key:
        concerts = enrich_with_openers(concerts, TicketmasterClient(config.ticketmaster_api_key, cache))

    def text(rows):
        event = None
        for r in rows:
            if (r['date'], r['event']) != event:
                event = (r['date'], r['event'])
                print(f"\n{r['date']}  {r['event']}  @ {r['venue']}")
            target = r['spotify_name'] or '—'
            line = f"  {r['billed_as']:<30} {r['role']:<9} {r['source']:<15} {r['method']:<18} {target}"
            if r['artist_id']:
                line += f"  ({r['artist_id']})"
            print(line)
            if r['latest_releases']:
                print(f"      {'; '.join(r['latest_releases'])}")

    _emit(resolution_rows(concerts, cache), args.json, text)


def cmd_candidates(cache: Cache, args) -> None:
    def text(result):
        cur = result['current']
        print(f"\"{result['name']}\" currently: {cur['method']} {cur['artist_id'] or ''}")
        print('\nSpotify:')
        for c in result['spotify']:
            marker = '*' if c['current'] else ' '
            releases = c['releases'] if isinstance(c['releases'], str) else '; '.join(c['releases']) or 'no releases'
            print(f" {marker} {c['name']:<28} {c['artist_id']}  match {c['match_score']:.0f}  plays {c['your_plays']}")
            print(f"      {releases}")
        if 'setlist_fm' in result:
            print('\nsetlist.fm:')
            for a in result['setlist_fm'] or [{'mbid': '', 'name': 'no exact-name artists', 'disambiguation': None}]:
                print(f"   {a['name']:<28} {a['mbid']}  {a['disambiguation'] or ''}")

    _emit(find_candidates(args.name, _spotify(), cache, config.setlist_fm_api_key), args.json, text)


def cmd_pin(cache: Cache, args) -> None:
    if bool(args.artist_id) == args.none:
        sys.exit('Give either a SPOTIFY_ID or --none.')

    spotify_name = None
    if args.artist_id:
        spotify_name = _spotify()._get(f'artists/{args.artist_id}')['name']

    pins.pin_artist(cache, args.name, args.artist_id, args.until)
    # Drop the old answer so an unpin starts from a fresh search rather than
    # falling back to the result the pin was correcting.
    cache.delete(f'artist_resolve:{args.name.lower().strip()}')

    result = {'name': args.name, 'artist_id': args.artist_id, 'spotify_name': spotify_name,
              'until': args.until}
    _emit(result, args.json, lambda r: print(
        f"Pinned \"{r['name']}\" → {r['spotify_name'] or 'no artist'}"
        + (f" ({r['artist_id']})" if r['artist_id'] else '')
        + (f" until {r['until']}" if r['until'] else '')
    ))


def cmd_unpin(cache: Cache, args) -> None:
    removed = pins.unpin_artist(cache, args.name)
    _emit({'name': args.name, 'removed': removed}, args.json, lambda r: print(
        f"Unpinned \"{r['name']}\"" if r['removed'] else f"No pin for \"{r['name']}\""
    ))


def _drop_setlist_cache(cache: Cache, artist_id: str, name: str) -> None:
    for n in {name, cached_artist_name(artist_id, cache) or name}:
        cache.delete(f'setlist:{n.lower().strip()}')


def cmd_pin_setlist(cache: Cache, args) -> None:
    artist_id = _require_artist(cache, args.name)
    pins.pin_setlist_mbid(cache, artist_id, args.mbid)
    _drop_setlist_cache(cache, artist_id, args.name)
    _emit({'name': args.name, 'artist_id': artist_id, 'mbid': args.mbid}, args.json, lambda r: print(
        f"setlist.fm for \"{r['name']}\" ({r['artist_id']}) pinned to {r['mbid']}"
    ))


def cmd_unpin_setlist(cache: Cache, args) -> None:
    artist_id = _require_artist(cache, args.name)
    removed = pins.unpin_setlist_mbid(cache, artist_id)
    if removed:
        _drop_setlist_cache(cache, artist_id, args.name)
    _emit({'name': args.name, 'artist_id': artist_id, 'removed': removed}, args.json, lambda r: print(
        f"Unpinned setlist.fm for \"{r['name']}\"" if r['removed'] else f"No setlist.fm pin for \"{r['name']}\""
    ))


def cmd_pins(cache: Cache, args) -> None:
    today = date.today()
    data = {
        'artists': [
            {'name': p.name, 'artist_id': p.artist_id,
             'spotify_name': cached_artist_name(p.artist_id, cache) if p.artist_id else None,
             'until': p.until, 'expired': p.until is not None and p.until < today, 'added_at': p.added_at}
            for p in pins.list_artist_pins(cache)
        ],
        'setlist_fm': [
            {'artist_id': aid, 'spotify_name': cached_artist_name(aid, cache), 'mbid': mbid}
            for aid, mbid in pins.list_setlist_mbid_pins(cache).items()
        ],
    }

    def text(d):
        if not d['artists'] and not d['setlist_fm']:
            print('No pins.')
        for p in d['artists']:
            target = f"{p['spotify_name'] or '?'} ({p['artist_id']})" if p['artist_id'] else 'no artist'
            until = f"  until {p['until']}{' (expired)' if p['expired'] else ''}" if p['until'] else ''
            print(f"  {p['name']:<30} → {target}{until}")
        for p in d['setlist_fm']:
            print(f"  setlist.fm {p['spotify_name'] or p['artist_id']:<19} → {p['mbid']}")

    _emit(data, args.json, text)


def cmd_refresh(cache: Cache, args) -> None:
    def text(r):
        print(f"\"{r['name']}\" ({r['method']}{', ' + r['artist_id'] if r['artist_id'] else ''}): "
              f"removed {len(r['removed'])} cache entries")
        for key in r['removed']:
            print(f'  {key}')

    _emit(refresh_artist(cache, args.name), args.json, text)


def cmd_audit(cache: Cache, args) -> None:
    artist_id = _require_artist(cache, args.name)
    tracks = cached_artist_tracks(artist_id, cache)
    if tracks is None:
        sys.exit(f'No cached discography for "{args.name}". Run --dry-run first.')
    canonical = (cached_artist_name(artist_id, cache) or args.name).lower().strip()
    setlist = cache.get(f'setlist:{canonical}')
    lastfm = cache.get(f'lastfm:{canonical}')

    result = audit_artist(tracks, setlist or {}, lastfm or {})
    result = {'name': args.name, 'artist_id': artist_id,
              'setlist_cached': setlist is not None, 'lastfm_cached': lastfm is not None, **result}

    def text(r):
        print(f"\"{r['name']}\" ({r['artist_id']}): {r['tracks']} tracks in discography")
        for source in ('setlist', 'lastfm'):
            if not r[f'{source}_cached']:
                print(f'\n{source}: not cached (run --dry-run)')
                continue
            print(f"\n{source} names matching no track: {len(r[f'{source}_unmatched'])}")
            for m in r[f'{source}_unmatched']:
                hint = (f"  folds to \"{m['folds_to']}\"" if 'folds_to' in m
                        else f"  close to \"{m['close_to']}\"" if 'close_to' in m else '')
                print(f"  {m['score']:.2f}  {m['stored_as']}{hint}")
        if r['possible_duplicates']:
            print('\nPossible duplicates (same base title, different lookup keys):')
            for group in r['possible_duplicates']:
                print(f"  {' | '.join(group)}")
        print(f"\nTracks with no setlist or Last.fm signal: {len(r['tracks_without_signal'])}")

    _emit(result, args.json, text)


def main() -> None:
    parser = argparse.ArgumentParser(prog='python -m repair', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument('--json', action='store_true', help='Machine-readable output')
        p.set_defaults(fn=fn)
        return p

    add('resolutions', cmd_resolutions, 'How every upcoming billed name resolved')
    add('candidates', cmd_candidates, 'Every artist a name could be').add_argument('name')
    p = add('pin', cmd_pin, 'Force a name to an artist, or to none')
    p.add_argument('name')
    p.add_argument('artist_id', nargs='?')
    p.add_argument('--none', action='store_true', help='The act has no Spotify profile')
    p.add_argument('--until', type=date.fromisoformat, metavar='YYYY-MM-DD',
                   help='Ignore the pin after this date (e.g. the last show, for --none)')
    add('unpin', cmd_unpin, 'Remove a name pin').add_argument('name')
    p = add('pin-setlist', cmd_pin_setlist, "Force the setlist.fm artist for a name's Spotify artist")
    p.add_argument('name')
    p.add_argument('mbid')
    add('unpin-setlist', cmd_unpin_setlist, 'Remove a setlist.fm pin').add_argument('name')
    add('pins', cmd_pins, 'List every pin')
    add('refresh', cmd_refresh, "Drop one artist's cached data").add_argument('name')
    add('audit', cmd_audit, 'Song names that match no Spotify track').add_argument('name')

    args = parser.parse_args()
    args.fn(Cache(), args)


if __name__ == '__main__':
    main()
