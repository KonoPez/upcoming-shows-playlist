#!/usr/bin/env python3
"""
Concert Playlist — auto-update a Spotify playlist from your upcoming concerts.

Commands:
  --setup    First-time configuration and Spotify authentication.
  --update   Fetch concerts, score tracks, and update the playlist.
             This is what your weekly cron job should call.
  --status   Print upcoming concerts without modifying anything.
  --dry-run  Show the planned playlist (per-artist track lists) without
             actually writing to Spotify.
"""

import argparse
import datetime
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from config import config, APP_DIR
from cache import Cache
from artist_resolver import resolve_artist
from sources.models import Artist, Concert, Track
from sources.apple_calendar import AppleCalendarClient
from sources.google_calendar import GoogleCalendarClient
from spotify_client.client import SpotifyClient
from spotify_client.auth import get_spotify_client
from playlist_logic.weighting import compute_artist_weights, allocate_slots
from playlist_logic.scoring import select_tracks_for_artist
from playlist_logic.discovery_weighting import (
    compute_artist_familiarity_scores,
    select_discovery_artists,
    compute_discovery_weights,
)
from sources.setlist import SetlistClient
from sources.lastfm import LastFmClient
from sources.ticketmaster import TicketmasterClient, enrich_with_openers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

ENV_FILE = Path('.env')


# ── Setup ────────────────────────────────────────────────────────────────────

def cmd_setup() -> None:
    print('\n=== Concert Playlist — Setup ===\n')

    def prompt(label: str, current: str = '', required: bool = True) -> str:
        suffix = '' if required else ' (optional — press Enter to skip)'
        hint = f' [{current}]' if current else ''
        val = input(f'{label}{hint}{suffix}: ').strip()
        return val if val else current

    # ── Spotify ───────────────────────────────────────────────────────────────
    print('Spotify')
    print('  Create an app at https://developer.spotify.com/dashboard')
    print('  Set Redirect URI to http://127.0.0.1:8080  (no client secret needed)\n')
    spotify_id = prompt('Spotify Client ID', config.spotify_client_id)

    # Try silently with cached token first; only open browser if needed
    print()
    sp_raw = None
    try:
        sp_raw = get_spotify_client(
            client_id=spotify_id,
            redirect_uri='http://127.0.0.1:8080',
            token_path=config.spotify_token_path,
            open_browser=False,
        )
        print('Spotify: already authenticated ✓')
    except RuntimeError:
        print('Spotify: opening browser for authentication...')
        print('After authorising, paste the full redirect URL back here.\n')
        try:
            sp_raw = get_spotify_client(
                client_id=spotify_id,
                redirect_uri='http://127.0.0.1:8080',
                token_path=config.spotify_token_path,
                open_browser=True,
            )
            print('Spotify: authenticated ✓')
        except Exception as e:
            print(f'Spotify authentication failed: {e}')
            print('Re-run --setup to try again.')
            return

    # ── Playlist ──────────────────────────────────────────────────────────────
    sp = SpotifyClient(sp_raw)
    playlist_id = config.playlist_id
    if playlist_id:
        try:
            pl = sp_raw.playlist(playlist_id, fields='id,name')
            print(f'Playlist: using existing "{pl["name"]}" ✓')
        except Exception:
            print('Playlist: existing playlist not accessible, creating a new one...')
            playlist_id = sp.get_or_create_playlist(config.playlist_name or 'Concert Prep')
    else:
        playlist_name = prompt('Playlist name', 'Concert Prep', required=False) or 'Concert Prep'
        playlist_id = sp.get_or_create_playlist(playlist_name)
        print(f'Playlist: created "{playlist_name}" ✓')

    # ── Apple Calendar ────────────────────────────────────────────────────────
    print('\nApple Calendar (optional)')
    print('  Requires an app-specific password — NOT your Apple ID password.')
    print('  Generate one: https://appleid.apple.com → App-Specific Passwords\n')
    apple_user = prompt('iCloud email', config.apple_username, required=False)
    apple_pass = config.apple_app_password
    if apple_user and not apple_pass:
        apple_pass = prompt('App-specific password', '', required=False)

    # ── Google Calendar ───────────────────────────────────────────────────────
    print('\nGoogle Calendar (optional)')
    print('  Google Calendar → Settings (gear) → [your calendar] → Integrate calendar')
    print('  → copy "Secret address in iCal format"\n')
    google_ics = prompt('Google Calendar ICS URL', config.google_calendar_ics_url, required=False)

    # ── Ticketmaster ──────────────────────────────────────────────────────────
    print('\nTicketmaster (optional)')
    print('  Finds opening acts not listed in your calendar events.')
    print('  Get a free key at https://developer.ticketmaster.com/\n')
    tm_key = prompt('Ticketmaster API key', config.ticketmaster_api_key, required=False)

    # ── Setlist.fm ────────────────────────────────────────────────────────────
    print('\nSetlist.fm (optional)')
    print('  Boosts tracks that artists regularly play live.')
    print('  Get a free key at https://www.setlist.fm/settings/apps\n')
    setlist_key = prompt('Setlist.fm API key', config.setlist_fm_api_key, required=False)

    # ── Last.fm ───────────────────────────────────────────────────────────────
    print('\nLast.fm (optional)')
    print('  Scores tracks by global play count popularity (dominant signal when set).')
    print('  Get a free key at https://www.last.fm/api/account/create\n')
    lastfm_key = prompt('Last.fm API key', config.lastfm_api_key, required=False)

    # ── Write .env ────────────────────────────────────────────────────────────
    env_lines = [
        '# Concert Playlist Configuration',
        '# Generated by: python main.py --setup',
        '',
        '# Spotify',
        f'SPOTIFY_CLIENT_ID={spotify_id}',
        'SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080',
        '',
        '# Apple Calendar (optional)',
        f'APPLE_CALENDAR_USERNAME={apple_user}',
        f'APPLE_CALENDAR_APP_PASSWORD={apple_pass}',
        '',
        '# Google Calendar (optional)',
        f'GOOGLE_CALENDAR_ICS_URL={google_ics}',
        '',
        '# Ticketmaster (optional)',
        f'TICKETMASTER_API_KEY={tm_key}',
        '',
        '# Setlist.fm (optional)',
        f'SETLIST_FM_API_KEY={setlist_key}',
        '',
        '# Last.fm (optional)',
        f'LASTFM_API_KEY={lastfm_key}',
        '',
        '# Playlist',
        f'PLAYLIST_ID={playlist_id}',
        f'PLAYLIST_NAME={config.playlist_name or "Concert Prep"}',
    ]
    ENV_FILE.write_text('\n'.join(env_lines) + '\n')

    print(f'\nSetup complete. Configuration saved to {ENV_FILE}\n')
    print('Run the playlist update:')
    print('  python main.py --update\n')
    print('To install the daily cron job (runs at 9 AM):')
    print(f'  python {Path.cwd()}/setup_cron.py')


# ── Cache status ─────────────────────────────────────────────────────────────

def cmd_cache_status() -> None:
    s = Cache().get_summary()

    print('\n=== Cache Summary ===\n')

    last = s['last_run']
    if last:
        run_at = datetime.datetime.fromisoformat(last['run_at'])
        age = datetime.datetime.now(datetime.timezone.utc) - run_at
        hours, rem = divmod(int(age.total_seconds()), 3600)
        minutes = rem // 60
        age_str = f'{hours}h {minutes}m ago' if hours else f'{minutes}m ago'
        print(f'  Last run:     {run_at.strftime("%Y-%m-%d %H:%M UTC")}  ({age_str}, {last["trigger"]})')
    else:
        print('  Last run:     never')
    print(f'  Total runs:   {s["run_total"]}')

    print()
    print(f'  KV cache:     {s["kv_live"]} live entries, {s["kv_expired"]} expired')
    print(f'  Play history: {s["ph_plays"]} plays · {s["ph_tracks"]} tracks · {s["ph_artists"]} artists')
    print()


# ── Concert fetching ─────────────────────────────────────────────────────────

def fetch_all_concerts(window_days: int) -> "tuple[list[Concert], date]":
    today = date.today()
    end_date = today + timedelta(days=window_days)
    concerts: list[Concert] = []

    # Apple Calendar
    if config.apple_username and config.apple_app_password:
        logger.info('Fetching concerts from Apple Calendar…')
        apple = AppleCalendarClient(config.apple_username, config.apple_app_password)
        concerts.extend(apple.get_concerts(today, end_date))

    # Google Calendar
    if config.google_calendar_ics_url:
        logger.info('Fetching concerts from Google Calendar…')
        gcal = GoogleCalendarClient(config.google_calendar_ics_url)
        concerts.extend(gcal.get_concerts(today, end_date))

    return concerts, today


# ── Status ───────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    try:
        config.validate_required()
    except ValueError as e:
        print(f'Configuration incomplete:\n{e}')
        return

    concerts, today = fetch_all_concerts(config.concert_window_days)

    if config.ticketmaster_api_key:
        cache = Cache()
        tm = TicketmasterClient(config.ticketmaster_api_key, cache)
        concerts = enrich_with_openers(concerts, tm)

    if not concerts:
        print('No upcoming concerts found.')
        return

    by_date: dict[date, list[Concert]] = {}
    for c in concerts:
        by_date.setdefault(c.event_date, []).append(c)

    print(f'\nUpcoming concerts — next {config.concert_window_days} days\n')
    for concert_date in sorted(by_date):
        days = (concert_date - today).days
        print(f'  {concert_date}  ({days:>2}d away)')
        for c in by_date[concert_date]:
            print(f'    · {c.artist_name} @ {c.venue}  [{c.source}]')

    unique_artists = {c.artist_name for c in concerts}
    print(f'\n{len(concerts)} concerts · {len(unique_artists)} unique artists')


# ── Core playlist build ───────────────────────────────────────────────────────

def cmd_build(dry_run: bool = False, trigger: str = 'manual') -> None:
    config.validate_required()

    cache = Cache()
    cache.clear_expired()

    today = date.today()

    # 1. Fetch concerts from all configured calendar sources
    concerts, today = fetch_all_concerts(config.concert_window_days)
    if not concerts:
        logger.warning('No concerts found. Make sure your calendar is configured and contains concert events.')
        return

    # 1b. Enrich single-artist events with Ticketmaster opener data
    if config.ticketmaster_api_key:
        logger.info('Checking Ticketmaster for opening acts…')
        tm = TicketmasterClient(config.ticketmaster_api_key, cache)
        concerts = enrich_with_openers(concerts, tm)

    # 2. Authenticate with Spotify
    sp_raw = get_spotify_client(
        client_id=config.spotify_client_id,
        redirect_uri=config.spotify_redirect_uri,
        token_path=config.spotify_token_path,
        open_browser=False,
    )
    sp = SpotifyClient(sp_raw)

    # 3. Accumulate play history (run every time so familiarity improves weekly)
    logger.info('Syncing play history…')
    plays = sp.get_recently_played_with_artists(cache)
    new_plays = cache.record_plays(plays)
    logger.info(f'Recorded {new_plays} new play events')

    # 4. Resolve each artist name → Spotify artist ID; deduplicate concerts
    logger.info('Resolving artists to Spotify profiles…')
    artists: dict[str, Artist] = {}
    seen: set[tuple[str, date]] = set()

    for concert in concerts:
        spotify_id = resolve_artist(
            concert.artist_name,
            sp_raw,
            cache,
            tm_spotify_id=concert.tm_spotify_id,
        )
        if not spotify_id:
            continue

        dedup = (spotify_id, concert.event_date)
        if dedup in seen:
            continue
        seen.add(dedup)

        if spotify_id not in artists:
            artists[spotify_id] = Artist(spotify_id=spotify_id, name=concert.artist_name)
        artists[spotify_id].concerts.append(concert)

    logger.info(f'Resolved {len(artists)} unique artists')
    if not artists:
        logger.warning('No artists could be resolved to Spotify profiles.')
        return

    # 5. Compute weights and allocate duration budgets
    _ASSUMED_AVG_TRACK_MS = 210_000  # 3.5 min average track length
    weights = compute_artist_weights({a_id: a.concerts for a_id, a in artists.items()}, today)
    slots = allocate_slots(
        weights,
        target_duration_ms=config.playlist_target_duration_minutes * 60_000,
        min_duration_ms=config.min_tracks_per_artist * _ASSUMED_AVG_TRACK_MS,
    )

    # 6. Get user familiarity from Spotify API
    logger.info('Fetching Spotify listening history…')
    spotify_familiarity = sp.get_user_familiarity(cache)

    # 7. Select tracks per artist
    setlist_client = SetlistClient(config.setlist_fm_api_key, cache) \
        if config.setlist_fm_api_key else None
    lastfm_client = LastFmClient(config.lastfm_api_key, cache) \
        if config.lastfm_api_key else None

    for artist_id, duration_budget_ms in sorted(slots.items(), key=lambda x: -x[1]):
        artist = artists[artist_id]
        logger.info(f'  {artist.name}: fetching discography…')

        tracks = sp.get_artist_tracks(artist_id, cache)
        if not tracks:
            logger.warning(f'  {artist.name}: no tracks found on Spotify')
            continue

        play_counts = cache.get_play_counts(artist_id)
        artist.setlist_scores = setlist_client.get_setlist_scores(artist.name) if setlist_client else None
        artist.lastfm_scores  = lastfm_client.get_popularity_scores(artist.name) if lastfm_client else None
        artist.selected_tracks = select_tracks_for_artist(
            tracks=tracks,
            duration_budget_ms=duration_budget_ms,
            spotify_familiarity=spotify_familiarity,
            play_counts=play_counts,
            today=today,
            setlist_scores=artist.setlist_scores,
            lastfm_scores=artist.lastfm_scores,
        )
        chosen_ms = sum(t.duration_ms for t in artist.selected_tracks)
        logger.info(
            f'  {artist.name}: {len(artist.selected_tracks)} tracks (~{chosen_ms // 60_000}m of {duration_budget_ms // 60_000}m budget, '
            f'{len(tracks)} in discography, '
            f'concert in {min(c.days_until(today) for c in artist.concerts)}d)'
        )

    # 8. Flatten to ordered track list (nearest concert first)
    def nearest(artist_id: str) -> int:
        return min(c.days_until(today) for c in artists[artist_id].concerts)

    all_tracks: list[Track] = []
    for artist_id in sorted(slots, key=nearest):
        all_tracks.extend(artists[artist_id].selected_tracks)

    track_uris = [f'spotify:track:{t.id}' for t in all_tracks]
    total = len(track_uris)
    total_ms = sum(t.duration_ms for t in all_tracks)
    total_min = total_ms // 60_000

    # 9. Dry run: print summary and exit
    if dry_run:
        artists_with_tracks = sum(1 for a in artists.values() if a.selected_tracks)
        print(f'\n=== DRY RUN — {total} tracks (~{total_min}m) from {artists_with_tracks} artists ===\n')
        for artist_id in sorted(slots, key=nearest):
            artist = artists[artist_id]
            if not artist.selected_tracks:
                continue
            days = nearest(artist_id)
            chosen_min = sum(t.duration_ms for t in artist.selected_tracks) // 60_000
            print(f'{artist.name}  ({len(artist.selected_tracks)} tracks, ~{chosen_min}m, concert in {days}d):')
            sl_scores = artist.setlist_scores or {}
            lf_scores = artist.lastfm_scores or {}
            for t in artist.selected_tracks:
                release = (t.release_date or '?')[:4]
                dur = t.duration_ms // 1000
                name_key = t.name.lower().strip()
                freq = sl_scores.get(name_key)
                pop  = lf_scores.get(name_key)
                sl_str = f'{freq:.0%}' if freq is not None else '—'
                lf_str = f'{pop:.0%}'  if pop  is not None else '—'
                print(f'  [sl:{sl_str:>4} lf:{lf_str:>4}] {t.name}  ({t.album_name}, {release}) [{dur//60}:{dur%60:02d}]')
            print()
        return

    # 10. Update Spotify playlist
    playlist_id = sp.get_or_create_playlist(config.playlist_name, config.playlist_id)
    sp.update_playlist_tracks(playlist_id, track_uris)

    cache.record_run(trigger)

    print(f'\nPlaylist updated: {total} tracks (~{total_min}m) from {sum(1 for a in artists.values() if a.selected_tracks)} artists')
    print(f'Open: https://open.spotify.com/playlist/{playlist_id}')


# ── Discovery — location resolution ──────────────────────────────────────────

def _try_ip_geolocation() -> 'tuple[str, str] | None':
    """
    Attempt to resolve the current location via IP geolocation.
    Returns (latlong, city_label) on success, None on any failure.
    """
    import requests as _req
    try:
        resp = _req.get('http://ip-api.com/json', timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get('status') == 'success':
            lat  = data['lat']
            lon  = data['lon']
            city = data.get('city', '')
            return f'{lat},{lon}', city
    except Exception as e:
        logger.warning(f'IP geolocation failed: {e}')
    return None


def resolve_discovery_location(cache: 'Cache') -> 'tuple[str | None, str | None]':
    """
    Determine the location to use for concert discovery.
    Returns (latlong, city) with exactly one non-None.

    Resolution order on first run (no cached consent):
      1. Prompt for IP geolocation consent; cache the answer permanently.
      2. If consented and IP lookup succeeds → latlong.
      3. Otherwise prompt for city name; fall through to latlong prompt if skipped.
      4. If city skipped → prompt for lat,lng.

    On subsequent runs the cached consent answer is reused; IP is re-attempted
    each time if consented (location may change), falling back to config values
    or a prompt if it fails.
    """
    IP_CONSENT_KEY = 'ip_geo_consent'
    # ~10 years — effectively permanent; cleared by --clear-cache
    IP_CONSENT_TTL = 10 * 365 * 24 * 3600

    # Explicit config always takes priority — no prompting needed
    if config.discovery_lat_lng:
        return config.discovery_lat_lng, None
    if config.discovery_location:
        return None, config.discovery_location

    # No config: try IP geolocation (with one-time consent prompt)
    consent = cache.get(IP_CONSENT_KEY)
    if consent is None:
        answer = input(
            '\nMay we use your IP address to automatically detect your location? [y/N]: '
        ).strip().lower()
        consent = 'yes' if answer in ('y', 'yes') else 'no'
        cache.set(IP_CONSENT_KEY, consent, IP_CONSENT_TTL)

    if consent == 'yes':
        result = _try_ip_geolocation()
        if result:
            latlong, city_label = result
            logger.info(f'Location detected via IP: {city_label} ({latlong})')
            return latlong, None
        print('IP geolocation failed — please enter your location manually.')

    # Interactive fallback: prompt for city, then lat/lng
    city = input(
        'Enter your city for concert discovery (e.g. "Chicago, IL"), '
        'or press Enter to enter coordinates: '
    ).strip()
    if city:
        print(f'Tip: add DISCOVERY_LOCATION={city!r} to your .env to skip this prompt.')
        return None, city

    latlong = input('Enter coordinates as "lat,lng" (e.g. "41.85,-87.65"): ').strip()
    if latlong:
        print(f'Tip: add DISCOVERY_LAT_LNG={latlong!r} to your .env to skip this prompt.')
        return latlong, None

    raise ValueError(
        'Could not determine location for concert discovery. '
        'Set DISCOVERY_LOCATION or DISCOVERY_LAT_LNG in .env.'
    )


# ── Discovery playlist build ──────────────────────────────────────────────────

def resolve_calendar_ids(
    calendar_artist_names: list,
    sp_raw: object,
    cache: object,
) -> set:
    """
    Resolve a list of artist names (from calendar events) to Spotify artist IDs.

    Names that cannot be resolved are silently skipped.  The returned set is
    used to filter discovery candidates so already-booked artists are excluded.
    """
    ids: set = set()
    for name in calendar_artist_names:
        cid = resolve_artist(name, sp_raw, cache)
        if cid:
            ids.add(cid)
    return ids


_DESCRIPTION_MAX = 300


def _build_discovery_description(artists: dict, all_concerts: list) -> str:
    """
    Build a playlist description listing each event that contributed at least
    one artist to the playlist.

    Format per event:
        MM/DD Headliner (Openers: Opener1, Opener2) at Venue
        MM/DD Headliner at Venue   (when no selected openers)

    Headliner is taken from the full concert list regardless of selection so
    the event context is always clear.  Openers are only listed when they
    appear in the selected artists set.  Events are sorted by date.
    Events are joined with ' • ' and truncated to _DESCRIPTION_MAX chars.
    """
    from collections import defaultdict

    selected_names: set = {a.name for a in artists.values()}

    # Group the full TM list by (date, venue) to reconstruct bills
    bills: dict = defaultdict(lambda: {'headliner': None, 'selected_openers': [], '_seen_openers': set()})
    for concert in all_concerts:
        key = (concert.event_date, concert.venue)
        if not concert.is_opener:
            bills[key]['headliner'] = concert.artist_name
        elif concert.artist_name in selected_names:
            if concert.artist_name not in bills[key]['_seen_openers']:
                bills[key]['selected_openers'].append(concert.artist_name)
                bills[key]['_seen_openers'].add(concert.artist_name)

    lines = []
    for (event_date, venue), bill in sorted(bills.items()):
        headliner = bill['headliner']
        openers   = bill['selected_openers']

        headliner_selected = headliner in selected_names if headliner else False
        if not headliner_selected and not openers:
            continue  # no selected artist from this event

        date_str = event_date.strftime('%m/%d')
        if openers:
            opener_str = ', '.join(openers)
            lines.append(f'{date_str} {headliner} (Openers: {opener_str}) at {venue}')
        else:
            lines.append(f'{date_str} {headliner} at {venue}')

    description = ' • '.join(lines)
    if len(description) > _DESCRIPTION_MAX:
        description = description[:_DESCRIPTION_MAX - 1] + '…'
    return description


def cmd_discover(dry_run: bool = False, trigger: str = 'manual') -> None:
    config.validate_discovery()

    cache = Cache()
    cache.clear_expired()

    today = date.today()

    # 1. Resolve location
    latlong, city = resolve_discovery_location(cache)

    # 2. Fetch upcoming local concerts from Ticketmaster
    tm = TicketmasterClient(config.ticketmaster_api_key, cache)
    logger.info('Fetching local concerts from Ticketmaster…')
    concerts = tm.get_local_events(
        window_days=config.discovery_window_days,
        latlong=latlong,
        city=city,
        radius_miles=config.discovery_radius_miles,
    )

    if not concerts:
        print('No upcoming concerts found in your area. Try increasing DISCOVERY_RADIUS_MILES.')
        return

    logger.info(f'Found {len(concerts)} artist slots across local events')

    # 3. Collect calendar artist names to exclude (already have tickets)
    #     fetch_all_concerts returns empty if no calendar is configured.
    #     enrich_with_openers adds opening acts so they're excluded too —
    #     if you already have a ticket, you'll see the whole bill.
    logger.info('Fetching calendar events to identify already-booked artists…')
    cal_concerts, _ = fetch_all_concerts(config.discovery_window_days)
    if cal_concerts:
        cal_concerts = enrich_with_openers(cal_concerts, tm)
    seen_cal: set[str] = set()
    calendar_artist_names: list[str] = []
    for c in cal_concerts:
        if c.artist_name not in seen_cal:
            calendar_artist_names.append(c.artist_name)
            seen_cal.add(c.artist_name)
    if calendar_artist_names:
        logger.info(f'Found {len(calendar_artist_names)} calendar artist(s) to exclude')

    # 5. Authenticate with Spotify
    sp_raw = get_spotify_client(
        client_id=config.spotify_client_id,
        redirect_uri=config.spotify_redirect_uri,
        token_path=config.spotify_token_path,
        open_browser=False,
    )
    sp = SpotifyClient(sp_raw)

    # 6. Accumulate play history so familiarity scores improve over time
    logger.info('Syncing play history…')
    plays = sp.get_recently_played_with_artists(cache)
    new_plays = cache.record_plays(plays)
    logger.info(f'Recorded {new_plays} new play events')

    # 7. Resolve each artist name → Spotify artist ID; deduplicate
    logger.info('Resolving artists to Spotify profiles…')
    artists: dict[str, Artist] = {}
    seen: set[tuple[str, date]] = set()

    for concert in concerts:
        spotify_id = resolve_artist(
            concert.artist_name,
            sp_raw,
            cache,
            tm_spotify_id=concert.tm_spotify_id,
        )
        if not spotify_id:
            continue

        dedup = (spotify_id, concert.event_date)
        if dedup in seen:
            continue
        seen.add(dedup)

        if spotify_id not in artists:
            artists[spotify_id] = Artist(spotify_id=spotify_id, name=concert.artist_name)
        artists[spotify_id].concerts.append(concert)

    logger.info(f'Resolved {len(artists)} unique artists')
    if not artists:
        logger.warning('No artists could be resolved to Spotify profiles.')
        return

    # 8. Remove artists the user already has tickets to (calendar sources)
    if calendar_artist_names:
        calendar_ids = resolve_calendar_ids(calendar_artist_names, sp_raw, cache)
        before = len(artists)
        artists = {aid: a for aid, a in artists.items() if aid not in calendar_ids}
        removed = before - len(artists)
        if removed:
            logger.info(f'Excluded {removed} already-booked artist(s) from discovery candidates')
        if not artists:
            print('All nearby artists are already in your concert calendar — nothing new to discover!')
            return

    candidate_ids = list(artists.keys())

    # 9. Compute artist familiarity
    logger.info('Computing artist familiarity…')
    top_scores    = sp.get_artist_top_scores(cache)
    play_counts   = cache.get_artist_play_counts()
    familiarity   = compute_artist_familiarity_scores(candidate_ids, top_scores, play_counts)

    # 10. Fetch Last.fm global popularity (optional)
    raw_listeners: dict[str, int] = {}
    if config.lastfm_api_key:
        lastfm_client = LastFmClient(config.lastfm_api_key, cache)
        logger.info('Fetching Last.fm artist popularity…')
        for artist_id, artist in artists.items():
            count = lastfm_client.get_artist_listeners(artist.name)
            if count is not None:
                raw_listeners[artist_id] = count

    # 11. Score + select artists (floor + cap)
    selected_ids, enjoyment_scores = select_discovery_artists(
        candidate_ids=candidate_ids,
        familiarity_scores=familiarity,
        raw_listeners=raw_listeners,
        max_artists=config.discovery_max_artists,
        min_score=config.discovery_min_score,
    )

    if not selected_ids:
        print('No artists scored above the minimum threshold for your area.')
        return

    artists = {aid: artists[aid] for aid in selected_ids}

    # 12. Allocate duration budgets weighted by enjoyment × proximity
    weights = compute_discovery_weights(artists, enjoyment_scores, today)
    slots   = allocate_slots(
        weights,
        target_duration_ms=config.playlist_target_duration_minutes * 60_000,
        min_duration_ms=0,   # every selected artist gets a slot; select_tracks rounds up to 1 track
    )

    # 13. Get track-level familiarity for scoring
    logger.info('Fetching Spotify listening history…')
    spotify_familiarity = sp.get_user_familiarity(cache)

    # 14. Select tracks per artist (identical pipeline to prep playlist)
    setlist_client = SetlistClient(config.setlist_fm_api_key, cache) \
        if config.setlist_fm_api_key else None
    lastfm_track_client = LastFmClient(config.lastfm_api_key, cache) \
        if config.lastfm_api_key else None

    for artist_id, duration_budget_ms in sorted(slots.items(), key=lambda x: -x[1]):
        artist = artists[artist_id]
        logger.info(f'  {artist.name}: fetching discography…')

        tracks = sp.get_artist_tracks(artist_id, cache)
        if not tracks:
            logger.warning(f'  {artist.name}: no tracks found on Spotify')
            continue

        play_counts_artist = cache.get_play_counts(artist_id)
        artist.setlist_scores = setlist_client.get_setlist_scores(artist.name) if setlist_client else None
        artist.lastfm_scores  = lastfm_track_client.get_popularity_scores(artist.name) if lastfm_track_client else None
        artist.selected_tracks = select_tracks_for_artist(
            tracks=tracks,
            duration_budget_ms=duration_budget_ms,
            spotify_familiarity=spotify_familiarity,
            play_counts=play_counts_artist,
            today=today,
            setlist_scores=artist.setlist_scores,
            lastfm_scores=artist.lastfm_scores,
        )
        chosen_ms = sum(t.duration_ms for t in artist.selected_tracks)
        nearest   = min(c.days_until(today) for c in artist.concerts)
        logger.info(
            f'  {artist.name}: {len(artist.selected_tracks)} tracks '
            f'(~{chosen_ms // 60_000}m, enjoyment={enjoyment_scores[artist_id]:.2f}, '
            f'concert in {nearest}d)'
        )

    # 15. Flatten to ordered track list (highest-enjoyment artist first)
    all_tracks: list[Track] = []
    for artist_id in sorted(slots, key=lambda aid: -enjoyment_scores.get(aid, 0.0)):
        all_tracks.extend(artists[artist_id].selected_tracks)

    track_uris = [f'spotify:track:{t.id}' for t in all_tracks]
    total      = len(track_uris)
    total_min  = sum(t.duration_ms for t in all_tracks) // 60_000

    # 16. Dry run: print summary and exit
    if dry_run:
        artists_with_tracks = sum(1 for a in artists.values() if a.selected_tracks)
        print(f'\n=== DISCOVERY DRY RUN — {total} tracks (~{total_min}m) from {artists_with_tracks} artists ===\n')
        for artist_id in sorted(slots, key=lambda aid: -enjoyment_scores.get(aid, 0.0)):
            artist = artists[artist_id]
            if not artist.selected_tracks:
                continue
            nearest   = min(c.days_until(today) for c in artist.concerts)
            score     = enjoyment_scores[artist_id]
            chosen_min = sum(t.duration_ms for t in artist.selected_tracks) // 60_000
            venues    = ', '.join({c.venue for c in artist.concerts})
            print(f'{artist.name}  (enjoyment={score:.2f}, {len(artist.selected_tracks)} tracks, ~{chosen_min}m, concert in {nearest}d @ {venues}):')
            sl_scores = artist.setlist_scores or {}
            lf_scores = artist.lastfm_scores  or {}
            for t in artist.selected_tracks:
                release = (t.release_date or '?')[:4]
                dur     = t.duration_ms // 1000
                name_key = t.name.lower().strip()
                freq = sl_scores.get(name_key)
                pop  = lf_scores.get(name_key)
                sl_str = f'{freq:.0%}' if freq is not None else '—'
                lf_str = f'{pop:.0%}'  if pop  is not None else '—'
                print(f'  [sl:{sl_str:>4} lf:{lf_str:>4}] {t.name}  ({t.album_name}, {release}) [{dur//60}:{dur%60:02d}]')
            print()
        return

    # 17. Update Spotify discovery playlist
    playlist_id = sp.get_or_create_playlist(
        config.discovery_playlist_name,
        config.discovery_playlist_id,
    )

    # Persist playlist ID to .env if newly created
    if not config.discovery_playlist_id:
        _write_env_value('DISCOVERY_PLAYLIST_ID', playlist_id)
        config.discovery_playlist_id = playlist_id

    description = _build_discovery_description(artists, concerts)
    sp.update_playlist_description(playlist_id, description)
    sp.update_playlist_tracks(playlist_id, track_uris)
    cache.record_run(trigger)

    print(f'\nDiscovery playlist updated: {total} tracks (~{total_min}m) from {sum(1 for a in artists.values() if a.selected_tracks)} artists')
    print(f'Open: https://open.spotify.com/playlist/{playlist_id}')


def _write_env_value(key: str, value: str) -> None:
    """Append or update a key in the .env file."""
    env_path = ENV_FILE
    if not env_path.exists():
        env_path.write_text(f'{key}={value}\n')
        return
    lines = env_path.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f'{key}=') or line.startswith(f'{key} ='):
            lines[i] = f'{key}={value}'
            updated = True
            break
    if not updated:
        lines.append(f'{key}={value}')
    env_path.write_text('\n'.join(lines) + '\n')


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Concert Playlist — auto-update a Spotify playlist from upcoming concerts'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--setup',       action='store_true', help='First-time setup wizard')
    group.add_argument('--update',      action='store_true', help='Update the playlist (cron)')
    group.add_argument('--status',      action='store_true', help='Show upcoming concerts')
    group.add_argument('--dry-run',     action='store_true', help='Preview without modifying')
    group.add_argument('--clear-cache',        action='store_true', help='Clear all cached data (discographies, artist lookups)')
    group.add_argument('--cache-status',       action='store_true', help='Show cache stats and last run info')
    group.add_argument('--discover',           action='store_true', help='Build discovery playlist from local concerts')
    group.add_argument('--discover-dry-run',   action='store_true', help='Preview discovery playlist without writing to Spotify')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')
    parser.add_argument('--cron', action='store_true', help='Mark this run as cron-triggered (used in run log)')

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.setup:
        cmd_setup()
    elif args.cache_status:
        cmd_cache_status()
    elif args.clear_cache:
        counts = Cache().clear_all()
        print(f"Cache cleared ({counts['kv_cache']} entries removed). Play history preserved.")
    elif args.update:
        try:
            cmd_build(dry_run=False, trigger='cron' if args.cron else 'manual')
        except (ValueError, RuntimeError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
    elif args.dry_run:
        try:
            cmd_build(dry_run=True)
        except (ValueError, RuntimeError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
    elif args.status:
        cmd_status()
    elif args.discover:
        try:
            cmd_discover(dry_run=False, trigger='cron' if args.cron else 'manual')
        except (ValueError, RuntimeError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
    elif args.discover_dry_run:
        try:
            cmd_discover(dry_run=True)
        except (ValueError, RuntimeError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
