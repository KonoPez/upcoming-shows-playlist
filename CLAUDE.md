# Concert Playlist — Claude Code Guide

## What this project does

Two Spotify playlists, both auto-updated:

1. **Prep playlist** (`--update`): reads upcoming concert events from Apple Calendar / Google Calendar, resolves each artist to a Spotify profile, and fills the playlist with scored/weighted tracks to prep for each show.

2. **Discovery playlist** (`--discover`): queries Ticketmaster for upcoming concerts near the user's location, scores each artist by how much the user is likely to enjoy them, and fills a separate playlist with sample tracks from the top-scoring artists.

Both playlists can be scheduled via cron (`setup_cron.py` installs the jobs).

## Running the project

```bash
python main.py --setup              # first-time wizard: auth + config
python main.py --update             # fetch concerts, score tracks, write prep playlist (cron)
python main.py --dry-run            # preview prep playlist without writing to Spotify
python main.py --status             # print upcoming concerts, no writes
python main.py --discover           # fetch local concerts, score artists, write discovery playlist (cron)
python main.py --discover-dry-run   # preview discovery playlist without writing to Spotify
python main.py --cache-status       # show cache stats and last run info
python main.py --clear-cache        # clear all cached data (preserves play history and run log)
python main.py --update --cron      # mark this run as cron-triggered in the run log
```

## Running tests

```bash
python -m pytest tests/ -v
```

384 tests, no external dependencies required (no Spotify/calendar calls). Tests run in ~12 seconds.

## Project layout

```
main.py                         # CLI entrypoint, prep + discovery orchestration, location resolver
config.py                       # Config dataclass, loaded from .env via python-dotenv
cache.py                        # SQLite-backed KV cache (TTL) + play-history + run log
artist_resolver.py              # Calendar title parsing + Spotify artist ID resolution
sources/
  models.py                     # Concert, Track, Artist dataclasses
  apple_calendar.py             # iCloud CalDAV client
  google_calendar.py            # Google Calendar ICS client
  ticketmaster.py               # Ticketmaster Discovery API — opener enrichment + local event discovery
  setlist.py                    # Setlist.fm client — live play frequency per track
  lastfm.py                     # Last.fm client — track popularity scores + artist listener counts
spotify_client/
  auth.py                       # PKCE OAuth flow (no client secret)
  client.py                     # Playlist management, discography, play history, artist familiarity
playlist_logic/
  weighting.py                  # Exponential decay weights + Hamilton's slot allocation
  scoring.py                    # Track scoring: Last.fm popularity + setlist frequency + recency + novelty
  discovery_weighting.py        # Artist enjoyment scoring + discovery slot allocation
tests/
  test_artist_resolver.py       # Title parsing, artist splitting, Spotify search, resolve_artist
  test_playlist_logic.py        # Weighting, slot allocation, track scoring, album interleaving
  test_cache.py                 # KV TTL cache, play history accumulation
  test_spotify_client.py        # Variant recording filter (_is_variant_recording)
  test_ticketmaster.py          # Venue matching, Spotify ID extraction, opener enrichment
  test_setlist.py               # SetlistClient behaviour + setlist frequency in scoring
  test_lastfm.py                # LastFmClient behaviour + log-normalisation
  test_discovery.py             # Discovery scoring, familiarity, slot guarantee, TM local events
conftest.py                     # Adds project root to sys.path for test imports
setup_cron.py                   # Interactive cron installer — asks which playlists to schedule
.env                            # Local secrets — never commit
.env.example                    # Template with comments
```

## Configuration (.env)

### Prep playlist

| Variable | Required | Notes |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | From developer.spotify.com/dashboard |
| `SPOTIFY_REDIRECT_URI` | No | Defaults to `http://127.0.0.1:8080` |
| `APPLE_CALENDAR_USERNAME` | One of these | iCloud email |
| `APPLE_CALENDAR_APP_PASSWORD` | One of these | App-specific password (not Apple ID password) |
| `GOOGLE_CALENDAR_ICS_URL` | One of these | Secret iCal URL from Google Calendar settings |
| `PLAYLIST_ID` | No | Auto-written by `--setup` |
| `PLAYLIST_NAME` | No | Defaults to `Concert Prep` |
| `TICKETMASTER_API_KEY` | No | Enables opener-act lookup; free key at developer.ticketmaster.com |
| `SETLIST_FM_API_KEY` | No | Boosts tracks artists regularly play live; free key at setlist.fm/settings/apps |
| `LASTFM_API_KEY` | No | Scores tracks by global play count popularity (dominant signal when set); free key at last.fm/api/account/create |

At least one calendar source must be configured for the prep playlist.

### Discovery playlist

| Variable | Required | Notes |
|---|---|---|
| `TICKETMASTER_API_KEY` | Yes | Required for discovery (concert source) |
| `DISCOVERY_PLAYLIST_ID` | No | Auto-written after first `--discover` run |
| `DISCOVERY_PLAYLIST_NAME` | No | Defaults to `Concert Discoveries` |
| `DISCOVERY_LOCATION` | No | City string e.g. `Madison, WI` — used if IP geo declined/unavailable |
| `DISCOVERY_LAT_LNG` | No | Explicit coordinates e.g. `43.07,-89.40` — fallback if city fails |
| `DISCOVERY_RADIUS_MILES` | No | Defaults to 50 |
| `DISCOVERY_WINDOW_DAYS` | No | Defaults to 60 |

On the first `--discover` run the user is asked for IP geolocation consent; the answer is cached permanently in the KV store (cleared by `--clear-cache`). If declined or if IP lookup fails, the user is prompted for a city string or lat/lng, with a tip to save it in `.env`.

## Key design decisions

**Spotify auth**: PKCE flow — no client secret needed. Token stored at `~/.concert-playlist/spotify_token.json`.

**Ticketmaster opener enrichment** (`sources/ticketmaster.py`): After calendar sources are fetched, any event with fewer than 2 artists is looked up on the Ticketmaster Discovery API. The event is matched by keyword (headliner name) + date, then scored by whether the headliner appears in the attraction list (+10) and whether the venue fuzzy-matches (+5). A score ≥ 10 is required to accept the match. All non-headliner attractions become additional `Concert` objects with `source='ticketmaster'`; their `event_name` is overwritten with the headliner's calendar event name so they are treated as part of the same concert, not separate events. If Ticketmaster provides a Spotify external link for an opener, that ID is passed directly to `resolve_artist` (skipping search). Results are cached 7 days. API errors are not cached so the next run retries. Runs in both `--update`/`--dry-run` and `--status`.

**Ticketmaster local event discovery** (`TicketmasterClient.get_local_events`): Queries the Discovery API with a geo location (latlong or city) + radius + date range. Returns one `Concert` object per attraction per event; the first attraction is treated as the headliner (`is_opener=False`), remaining ones as openers (`is_opener=True`). Source is `'ticketmaster_discovery'`. Paginates up to `LOCAL_EVENTS_MAX_PAGES` (5 pages × 100 events = up to 500 events). Results cached 6 hours.

**Artist resolution**: Calendar title → artist name via `artist_resolver.extract_artist_from_calendar_title` (strips "Ticket(s): " prefixes, venue/tour suffixes, etc.), then `split_artist_names` for multi-artist bills. `split_artist_names` only splits on bare commas/& when the structure unambiguously signals a list (≥2 commas, or comma + conjunction) — a single lone comma is kept intact to avoid breaking band names like "Black Country, New Road". Spotify search uses quoted phrases (`artist:"Name"`) to prevent Lucene splitting on commas. Results cached 90 days; failures cached 1 day so bug fixes take effect quickly.

**Spotify API wrappers**: Several spotipy wrapper methods pass `None` kwargs (e.g. `market=None`, `country=None`) which get serialized as the string `"None"` in query params, causing 400/403 errors. Affected methods use `sp._get()` directly with params embedded in the URL string: `_get_artist_albums` (avoids `album%2Csingle` encoding and `country=None`), `_get_popularity_batch` (avoids `market=None` 403). `_get_artist_albums` also retries with `limit //= 2` on 400 for artists with non-standard limit caps; spotipy's logger is suppressed only during the retry attempt to avoid noisy ERROR logs for handled errors.

**Variant recording filter**: `client._is_variant_recording(track_name, album_name)` skips live, acoustic, unplugged, remix, instrumental, and demo recordings before they enter the scoring pipeline. Album-level check (e.g. "Live at X", "Unplugged", "Remixes") skips the whole album. Track-level check matches both parenthetical suffixes (e.g. "Song (Live)") and dash suffixes (e.g. "Song - Acoustic", "Song - Demo") to avoid false positives on artistic titles like "Live Wire".

**Track scoring** (see `playlist_logic/scoring.py`): Fixed base weights — Last.fm 45%, setlist 25%, recency 15%, novelty 15% — normalised by the sum of weights for signals that are actually available. This means relative signal importance is preserved regardless of which APIs are configured; no separate fallback weight sets are needed. Last.fm popularity (log-normalised global play counts from `artist.getTopTracks`) is the dominant signal when available. Setlist frequency (0.0–1.0) is how often the artist plays that track across their last 10 shows within the past year, sourced from setlist.fm. When setlist data is available but a track was never played live, it is penalised (the weight enters the denominator with a zero contribution), reflecting that live omission is meaningful signal. Recency is a linear decay over 18 months. Novelty is the inverse of familiarity. Familiarity combines Spotify top-tracks API signal and local play-count history, taking the max. A per-album cap (default 6 tracks) prevents a single album from dominating; selected tracks are then interleaved across albums (newest first, round-robin) to avoid consecutive same-album runs.

**Setlist.fm integration** (`sources/setlist.py`): `SetlistClient.get_setlist_scores(artist_name)` fetches up to 10 recent shows (within the past year) via `GET /search/setlists?artistName=`. Each song appearance is counted — a song played twice in one show (e.g. as an encore) counts twice, since repeated performance is meaningful signal. Frequency = appearances / shows_analysed, and can exceed 1.0. A 1-second sleep is inserted before each API call to respect rate limits. Results cached 7 days; empty results are also cached to avoid re-hitting the API for artists with no data.

**Last.fm integration** (`sources/lastfm.py`): `get_popularity_scores(artist_name)` fetches up to 50 top tracks via `artist.getTopTracks`; play counts are log-normalised relative to the artist's most-played track. `get_artist_listeners(artist_name)` fetches total listener count via `artist.getInfo` for use as a global popularity signal in discovery scoring; returns `None` if unavailable, caches `-1` as a sentinel so failed lookups aren't retried within the TTL. Both methods cache 7 days. In `--dry-run`/`--discover-dry-run` output, `sl` shows setlist frequency and `lf` shows Last.fm popularity score; `—` means no data for that track.

**Prep playlist slot allocation** (see `playlist_logic/weighting.py`): Exponential decay with 21-day half-life. `allocate_slots` distributes a total duration budget (default 2 hours) proportionally across artists. Artists whose proportional share is below `min_tracks_per_artist × 3.5 min` are excluded. Hamilton's method distributes rounding so budgets sum to the target exactly. `select_tracks_for_artist` greedily fills each artist's time budget by score, stopping once the accumulated `duration_ms` reaches the artist's budget (rounding up to the last whole track).

**Discovery artist scoring** (see `playlist_logic/discovery_weighting.py`):

*Artist enjoyment score*:
```
enjoyment = 0.75 * personal_familiarity + 0.25 * global_popularity
```
Degrades gracefully when Last.fm is not configured (familiarity carries full weight). `personal_familiarity` = `max(spotify_top_artists_score, play_history_score)`, where play history is log-normalised within the candidate set (not globally), so an artist with 3 plays scores meaningfully even if the user's overall most-played artist has 500 plays. `global_popularity` = Last.fm listener count (from `artist.getInfo`), log-normalised across the candidate set.

These weights are intentionally permissive — the min score floor (0.10) is kept low because options are limited without more signals. Both should be revisited when additional signals (live show quality, artist similarity) are added.

*Artist selection*: candidates below the min score floor (0.10) are excluded; the remaining artists are capped at `discovery_max_artists` (default 10), sorted by enjoyment score descending.

*Slot allocation*:
```
allocation_weight = enjoyment^1.5 * proximity^1.0
```
`min_duration_ms=0` is passed to `allocate_slots` so every selected artist always receives a non-zero budget and therefore at least 1 track.

**Artist familiarity (discovery)** (`SpotifyClient.get_artist_top_scores`): mirrors `get_user_familiarity` but at artist level. Short-term top artists → 1.0, medium-term → 0.8, long-term → 0.6. Cached 6 hours. Combined with play-history scores via `max()` in `compute_artist_familiarity_scores`.

**Cache** (`~/.concert-playlist/cache.db`): SQLite with three tables:
- `kv_cache` — TTL-based, stores artist resolutions, discographies, API responses
- `play_history` — append-only, accumulates plays across runs to improve novelty/familiarity scores over time
- `run_log` — records each successful `--update` or `--discover` run with timestamp and trigger (`'manual'` or `'cron'`); readable via `--cache-status`; the `--cron` flag sets the trigger. IP geolocation consent is stored in `kv_cache` and cleared by `--clear-cache`.

**Cron setup** (`setup_cron.py`): interactive — asks whether to schedule the prep playlist, the discovery playlist, or both. Each gets its own crontab entry with a distinct marker comment so they can be installed/removed independently. Both use `--cron` flag so runs are correctly tagged in the run log.

**Deprecated Spotify API fields/endpoints** — do not use these:
- `track.popularity` — returns 0 for most artists; use Last.fm `artist.getTopTracks` play counts instead
- `artist.popularity` — deprecated; same replacement
- `artist.genres` — deprecated; do not use for genre affinity or any other purpose
- `related-artists` endpoint — deprecated; artist similarity must be derived from other signals (Last.fm `artist.getSimilar`, play history co-occurrence)

## Tuning knobs (config.py)

```python
# Prep playlist
concert_window_days: int = 90                  # how far ahead to look for concerts
playlist_target_duration_minutes: int = 120    # target total playlist length
min_tracks_per_artist: int = 2                 # min tracks worth of budget; artists below excluded

# Discovery playlist
discovery_radius_miles: int = 50               # geo search radius for Ticketmaster
discovery_window_days: int = 60                # how far ahead to look for local concerts
discovery_max_artists: int = 10                # hard cap on artists selected
discovery_min_score: float = 0.10             # minimum enjoyment score to be included
```
