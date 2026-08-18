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
python main.py --block-artist NAME  # block an artist from future discovery runs (matches by name against current playlist)
python main.py --unblock-artist NAME # remove an artist from the discovery blocklist
python main.py --list-blocked       # list all blocked artists
python main.py --add-concert        # interactively add a manual concert (calendar-independent)
python main.py --remove-concert     # interactively remove a manual concert
python main.py --list-concerts      # list manually added concerts
python main.py --cache-status       # show cache stats and last run info
python main.py --clear-cache        # clear all cached data (preserves play history and run log)
python main.py --update --cron      # mark this run as cron-triggered in the run log
```

## Running tests

```bash
python -m pytest tests/ -v
```

483 tests, no external dependencies required (no Spotify/calendar calls). Tests run in ~20 seconds.

## Project layout

```
main.py                         # CLI entrypoint, prep + discovery orchestration, location resolver
config.py                       # Config dataclass, loaded from .env via python-dotenv
cache.py                        # SQLite-backed KV cache (TTL) + play-history + run log
artist_resolver.py              # Calendar title parsing + Spotify artist ID resolution
track_names.py                  # Track title normalisation (variant suffix stripping)
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
  test_track_names.py           # Track title normalisation
  test_playlist_logic.py        # Weighting, slot allocation, track scoring, album interleaving
  test_cache.py                 # KV TTL cache, play history accumulation
  test_spotify_client.py        # Variant filter, album-vs-single preference, dedup, canonical names
  test_ticketmaster.py          # Venue matching, Spotify ID extraction, opener enrichment
  test_setlist.py               # SetlistClient behaviour + setlist frequency in scoring
  test_lastfm.py                # LastFmClient behaviour + log-normalisation
  test_discovery.py             # Discovery scoring, familiarity, slot guarantee, TM local events
conftest.py                     # Adds project root to sys.path for test imports
setup_cron.py                   # Interactive cron installer — asks which playlists to schedule
debug_artist_scores.py          # Dev tool: print per-signal track scores for one artist
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
| `LASTFM_API_KEY` | No | Scores tracks by global play count popularity (secondary signal); free key at last.fm/api/account/create |

At least one calendar source must be configured for the prep playlist.

### Discovery playlist

| Variable | Required | Notes |
|---|---|---|
| `TICKETMASTER_API_KEY` | Yes | Required for discovery (concert source) |
| `DISCOVERY_PLAYLIST_ID` | No | Auto-written after first `--discover` run |
| `DISCOVERY_PLAYLIST_NAME` | No | Defaults to `Concert Discoveries` |
| `DISCOVERY_LOCATION` | No | City string e.g. `Madison, WI` — used if IP geo declined/unavailable |
| `DISCOVERY_LAT_LNG` | No | Explicit coordinates e.g. `43.07,-89.40` — fallback if city fails |

On the first `--discover` run the user is asked for IP geolocation consent; the answer is cached permanently in the KV store (cleared by `--clear-cache`). If declined or if IP lookup fails, the user is prompted for a city string or lat/lng, with a tip to save it in `.env`.

## Key design decisions

**Spotify auth**: PKCE flow — no client secret needed. Token stored at `~/.concert-playlist/spotify_token.json`.

**Ticketmaster opener enrichment** (`sources/ticketmaster.py`): After calendar sources are fetched, any event with fewer than 2 artists is looked up on the Ticketmaster Discovery API. The event is matched by keyword (headliner name) + date, then scored by whether the headliner appears in the attraction list (+10) and whether the venue fuzzy-matches (+5). A score ≥ 10 is required to accept the match. All non-headliner attractions become additional `Concert` objects with `source='ticketmaster'`; their `event_name` is overwritten with the headliner's calendar event name so they are treated as part of the same concert, not separate events. If Ticketmaster provides a Spotify external link for an opener, that ID is passed directly to `resolve_artist` (skipping search). Results are cached 7 days. API errors are not cached so the next run retries. Runs in both `--update`/`--dry-run` and `--status`.

**Ticketmaster local event discovery** (`TicketmasterClient.get_local_events`): Queries the Discovery API with a geo location (latlong or city) + radius + date range. Returns one `Concert` object per attraction per event; the first attraction is treated as the headliner (`is_opener=False`), remaining ones as openers (`is_opener=True`). Source is `'ticketmaster_discovery'`. Paginates up to `LOCAL_EVENTS_MAX_PAGES` (5 pages × 100 events = up to 500 events). Results cached 6 hours.

**Artist resolution**: Calendar title → artist name via `artist_resolver.extract_artist_from_calendar_title` (strips "Ticket(s): " prefixes, venue/tour suffixes, etc.), then `split_artist_names` for multi-artist bills. `split_artist_names` only splits on bare commas/& when the structure unambiguously signals a list (≥2 commas, or comma + conjunction) — a single lone comma is kept intact to avoid breaking band names like "Black Country, New Road". Anything following a `w/` or `feat.` is a support list, so a bare conjunction there *is* a separator ("Prince Daddy and the Hyena w/ Combat and Walter Etc." → 3 artists). A conjunction followed by "the" is never a separator, since "X and the Y" is overwhelmingly one band name. Spotify search uses quoted phrases (`artist:"Name"`) to prevent Lucene splitting on commas. `_normalize` folds `&` → `and` so the calendar's spelling matches Spotify's. Results cached 90 days; failures cached 1 day so bug fixes take effect quickly.

**Headliner vs opener from calendar titles**: both calendar clients treat position 0 of `split_artist_names` as the headliner (`is_opener=False`) and everything after it as supporting acts (`is_opener=True`) — bills are written headliner-first, and this matches what `TicketmasterClient.get_local_events` already does with attraction lists. Without this the whole bill defaulted to `is_opener=False` and the `HEADLINER_BONUS` in `compute_artist_weights` applied to every artist, cancelling itself out.

**Canonical artist names** (`SpotifyClient.get_artist_names`, applied by `main._apply_canonical_names`): after resolution, each artist's calendar/Ticketmaster-derived name is replaced with Spotify's own spelling, because setlist.fm and Last.fm index by canonical name — "Prince Daddy and the Hyena" 404s on setlist.fm while "Prince Daddy & the Hyena" has 746 setlists. One request per artist (the batch `artists?ids=` endpoint returns 403 for PKCE apps), cached 90 days; failures are not cached. Prep applies it right after resolution; discovery applies it *after* artist selection, since the candidate pool can run to several hundred.

**Spotify API wrappers**: Several spotipy wrapper methods pass `None` kwargs (e.g. `market=None`, `country=None`) which get serialized as the string `"None"` in query params, causing 400/403 errors. Affected methods use `sp._get()` directly with params embedded in the URL string: `_get_artist_albums` (avoids `album%2Csingle` encoding and `country=None`), `_get_popularity_batch` (avoids `market=None` 403), `get_playlist_artists` (avoids `market=None` 400). `_get_artist_albums` also retries with `limit //= 2` on 400 for artists with non-standard limit caps; spotipy's logger is suppressed only during the retry attempt to avoid noisy ERROR logs for handled errors. **General rule**: any spotipy wrapper that accepts `market`, `country`, or similar optional kwargs should be replaced with `sp._get()` using an inline query string.

**Variant recording filter**: `client._is_variant_recording(track_name, album_name)` skips live, acoustic, unplugged, remix, instrumental, and demo recordings before they enter the scoring pipeline. Album-level check (e.g. "Live at X", "Unplugged", "Remixes") skips the whole album. Track-level check matches both parenthetical suffixes (e.g. "Song (Live)") and dash suffixes (e.g. "Song - Acoustic", "Song - Demo") to avoid false positives on artistic titles like "Live Wire".

**Album version beats single version**: when the same song appears on several releases, the album cut wins — pre-release singles are often different mixes or edits, and album attribution is what album interleaving in scoring depends on. `Track.album_type` carries Spotify's `album_type`, ranked by `album_type_rank` as album (3) > compilation (2) > single (1) > unknown (0); note Spotify types EPs as `single`. The rank is applied at all three points where releases compete: `_get_artist_albums` (an album displaces a same-named single even if older), `_fetch_artist_tracks` (rank first, then oldest release as tiebreak), and `deduplicate_tracks` (candidates are narrowed to the best rank before the Last.fm / shortest-title tiebreaks). Rank is only consulted *after* the variant filter, so an album's live cut never outranks a studio single. Within one rank the **oldest** release still wins, so a later re-recording can't displace the original or inflate its recency score — that rule alone used to hand the song to the pre-release single (Combat's "Stay Golden" came from the *Epic Season Finale* single rather than the *Stay Golden* album). Release dates are compared through `pad_release_date`, since raw string compare ranks year-precision `"2020"` ahead of `"2020-01-01"`. The discography cache key is versioned (`artist_tracks:v2:`) so pre-fix entries are refetched rather than aged out over 30 days.

**Track scoring** (see `playlist_logic/scoring.py`): Fixed base weights — setlist 55%, Last.fm 10%, recency 10%, novelty 25% — normalised by the sum of weights for signals that are actually available. This means relative signal importance is preserved regardless of which APIs are configured; no separate fallback weight sets are needed. Setlist frequency is the dominant signal when available; Last.fm popularity (log-normalised global play counts from `artist.getTopTracks`) is a secondary signal. Setlist frequency (0.0–1.0) is how often the artist plays that track across their last 10 shows within the past year, sourced from setlist.fm. When setlist data is available but a track was never played live, it is penalised (the weight enters the denominator with a zero contribution), reflecting that live omission is meaningful signal. Recency is a linear decay over 18 months. Novelty is the inverse of familiarity. Familiarity combines Spotify top-tracks API signal and local play-count history, taking the max. Selected tracks are interleaved across albums (newest first, round-robin) to avoid consecutive same-album runs.

**Setlist.fm integration** (`sources/setlist.py`): `SetlistClient.get_setlist_scores(artist_name)` fetches up to `MAX_SHOWS` (10) shows via `GET /search/setlists?artistName=`. The age window is **relative to the artist's own most recent show**, not to today: the newest show with songs becomes the anchor, and shows more than `MAX_SPREAD_DAYS` (365) older than it are dropped. There is no absolute age floor — an artist who last toured two years ago still yields usable signal, which is better than none (Walter Etc. went from 0 shows analysed to 7 under this rule). The payoff is the handoff: the moment they play one show on a new tour, the anchor jumps forward and the entire stale run falls outside the window, so a single current setlist supersedes ten ancient ones. Shows with no songs entered yet are skipped *before* the anchor is chosen, so a blank listing can't drag the window forward and hide the real data behind it; the response is sorted by date rather than trusting setlist.fm's ordering. Each song appearance is counted — a song played twice in one show (e.g. as an encore) counts twice, since repeated performance is meaningful signal. Frequency = appearances / shows_analysed, and can exceed 1.0. A 1-second sleep is inserted before each API call to respect rate limits. Results cached 7 days; empty results are also cached to avoid re-hitting the API for artists with no data.

**Last.fm integration** (`sources/lastfm.py`): `get_popularity_scores(artist_name)` fetches up to 50 top tracks via `artist.getTopTracks`; play counts are log-normalised relative to the artist's most-played track. `get_artist_listeners(artist_name)` fetches total listener count via `artist.getInfo` for use as a global popularity signal in discovery scoring; returns `None` if unavailable, caches `-1` as a sentinel so failed lookups aren't retried within the TTL. Both methods cache 7 days. In `--dry-run`/`--discover-dry-run` output, `sl` shows setlist frequency and `lf` shows Last.fm popularity score; `—` means no data for that track.

**Prep playlist slot allocation** (see `playlist_logic/weighting.py`): Exponential decay with 21-day half-life. `allocate_slots` distributes a total duration budget (default 2 hours) proportionally across artists. Every artist with a non-zero weight receives a budget — there is no minimum-budget floor, and `select_tracks_for_artist` always takes at least one track, so no artist on the bill is dropped. Hamilton's method distributes rounding so budgets sum to the target exactly. `select_tracks_for_artist` greedily fills each artist's time budget by score, taking each next track only when doing so lands **closer** to the budget than stopping would (i.e. while remaining budget ≥ half the track's duration). Rounding to nearest rather than always up matters at the playlist level: stopping only once the budget is *exceeded* overshoots by up to a full track per artist — averaging half a track each, which compounded into 15+ minutes over a 2-hour target across ten artists. The first track is always taken regardless of budget, so an artist holding a slot is never dropped. Per-artist error is now signed rather than always positive, so it largely cancels across the playlist (measured: 137m → 123m against a 120m target).

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
`allocate_slots` gives every selected artist a non-zero budget and therefore at least 1 track.

**Artist familiarity (discovery)** (`SpotifyClient.get_artist_top_scores`): mirrors `get_user_familiarity` but at artist level. Short-term top artists → 1.0, medium-term → 0.8, long-term → 0.6. Cached 6 hours. Combined with play-history scores via `max()` in `compute_artist_familiarity_scores`.

**Cache** (`~/.concert-playlist/cache.db`): SQLite with four tables:
- `kv_cache` — TTL-based, stores artist resolutions, discographies, API responses
- `play_history` — append-only, accumulates plays across runs to improve novelty/familiarity scores over time
- `run_log` — records each successful `--update` or `--discover` run with timestamp and trigger (`'manual'` or `'cron'`); readable via `--cache-status`; the `--cron` flag sets the trigger. IP geolocation consent is stored in `kv_cache` and cleared by `--clear-cache`.
- `discovery_blocklist` — permanent artist exclusion list; populated via `--block-artist`; not cleared by `--clear-cache`. Artists are keyed by Spotify ID and matched against the current discovery playlist by name.

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

# Discovery playlist
discovery_radius_miles: int = 50               # geo search radius for Ticketmaster
discovery_window_days: int = 60                # how far ahead to look for local concerts
discovery_max_artists: int = 10                # hard cap on artists selected
discovery_min_score: float = 0.10             # minimum enjoyment score to be included
```
