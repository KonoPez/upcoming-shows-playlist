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
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from config import config, APP_DIR
from cache import Cache
from artist_resolver import resolve_artist
from sources.models import Concert
from sources.apple_calendar import AppleCalendarClient
from sources.google_calendar import GoogleCalendarClient
from spotify_client.client import SpotifyClient
from spotify_client.auth import get_spotify_client
from spotify_client.client import SpotifyClient
from playlist_logic.weighting import compute_artist_weights, allocate_slots, ConcertSlot
from playlist_logic.scoring import select_tracks_for_artist

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


# ── Concert fetching ─────────────────────────────────────────────────────────

def fetch_all_concerts(window_days: int) -> tuple[list[Concert], date]:
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
        from sources.ticketmaster import TicketmasterClient, enrich_with_openers
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

def cmd_build(dry_run: bool = False) -> None:
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
        from sources.ticketmaster import TicketmasterClient, enrich_with_openers
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
    artist_names: dict[str, str] = {}
    artist_concerts: dict[str, list[ConcertSlot]] = {}
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

        days_until = (concert.event_date - today).days
        artist_names[spotify_id] = concert.artist_name
        artist_concerts.setdefault(spotify_id, []).append(ConcertSlot(days_until, concert.is_opener))

    logger.info(f'Resolved {len(artist_concerts)} unique artists')
    if not artist_concerts:
        logger.warning('No artists could be resolved to Spotify profiles.')
        return

    # 5. Compute weights and allocate track slots
    weights = compute_artist_weights(artist_concerts)
    slots = allocate_slots(
        weights,
        target_size=config.playlist_target_size,
        min_slots=config.min_tracks_per_artist,
    )

    # 6. Get user familiarity from Spotify API
    logger.info('Fetching Spotify listening history…')
    spotify_familiarity = sp.get_user_familiarity(cache)

    # 7. Select tracks per artist
    selected_by_artist: dict[str, list[dict]] = {}

    for artist_id, num_slots in sorted(slots.items(), key=lambda x: -x[1]):
        name = artist_names.get(artist_id, artist_id)
        logger.info(f'  {name}: fetching discography…')

        tracks = sp.get_artist_tracks(artist_id, cache)
        if not tracks:
            logger.warning(f'  {name}: no tracks found on Spotify')
            continue

        play_counts = cache.get_play_counts(artist_id)
        chosen = select_tracks_for_artist(
            tracks=tracks,
            num_slots=num_slots,
            spotify_familiarity=spotify_familiarity,
            play_counts=play_counts,
            today=today,
        )
        selected_by_artist[artist_id] = chosen
        logger.info(
            f'  {name}: {len(chosen)}/{num_slots} tracks selected '
            f'(from {len(tracks)} in discography, '
            f'concert in {min(s.days_until for s in artist_concerts[artist_id])}d)'
        )

    # 8. Flatten to ordered track list (nearest concert first)
    def nearest(artist_id: str) -> int:
        return min(s.days_until for s in artist_concerts[artist_id])

    all_tracks: list[dict] = []
    for artist_id in sorted(slots, key=nearest):
        all_tracks.extend(selected_by_artist.get(artist_id, []))

    track_uris = [f'spotify:track:{t["id"]}' for t in all_tracks]
    total = len(track_uris)

    # 9. Dry run: print summary and exit
    if dry_run:
        print(f'\n=== DRY RUN — {total} tracks from {len(selected_by_artist)} artists ===\n')
        for artist_id in sorted(slots, key=nearest):
            name = artist_names.get(artist_id, artist_id)
            days = nearest(artist_id)
            chosen = selected_by_artist.get(artist_id, [])
            print(f'{name}  ({len(chosen)} tracks, concert in {days}d):')
            for t in chosen:
                release = t.get('release_date', '?')[:4]
                pop = t.get('popularity', 0)
                print(f'  [{pop:>3}] {t["name"]}  ({t.get("album_name", "")}, {release})')
            print()
        return

    # 10. Update Spotify playlist
    playlist_id = sp.get_or_create_playlist(config.playlist_name, config.playlist_id)
    sp.update_playlist_tracks(playlist_id, track_uris)

    print(f'\nPlaylist updated: {total} tracks from {len(selected_by_artist)} artists')
    print(f'Open: https://open.spotify.com/playlist/{playlist_id}')


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
    group.add_argument('--clear-cache', action='store_true', help='Clear all cached data (discographies, artist lookups)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.setup:
        cmd_setup()
    elif args.clear_cache:
        counts = Cache().clear_all()
        print(f"Cache cleared ({counts['kv_cache']} entries removed). Play history preserved.")
    elif args.update:
        try:
            cmd_build(dry_run=False)
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


if __name__ == '__main__':
    main()
