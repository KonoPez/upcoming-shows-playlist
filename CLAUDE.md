# Concert Playlist — Claude Code Guide

## What this project does

Reads upcoming concert events from Apple Calendar (iCloud CalDAV) and/or Google Calendar (private ICS URL), resolves each artist to a Spotify profile, and updates a Spotify playlist with scored/weighted tracks to prep for each show. Runs daily via cron (`setup_cron.py` installs the job).

## Running the project

```bash
python main.py --setup      # first-time wizard: auth + config
python main.py --update     # fetch concerts, score tracks, write playlist (used by cron)
python main.py --status     # print upcoming concerts, no writes
python main.py --dry-run    # preview playlist without writing to Spotify
```

## Running tests

```bash
python -m pytest tests/ -v
```

311 tests, no external dependencies required (no Spotify/calendar calls). Tests run in ~10 seconds.

## Project layout

```
main.py                     # CLI entrypoint + concert-fetching orchestration
config.py                   # Config dataclass, loaded from .env via python-dotenv
cache.py                    # SQLite-backed KV cache (TTL) + play-history accumulation
artist_resolver.py          # Calendar title parsing + Spotify artist ID resolution
sources/
  models.py                 # Concert and Track dataclasses
  apple_calendar.py         # iCloud CalDAV client
  google_calendar.py        # Google Calendar ICS client
  ticketmaster.py           # Ticketmaster Discovery API client (opener enrichment)
  setlist.py                # Setlist.fm client — live play frequency per track
  lastfm.py                 # Last.fm client — global track popularity scores
spotify_client/
  auth.py                   # PKCE OAuth flow (no client secret)
  client.py                 # Playlist management, discography fetching, play history
playlist_logic/
  weighting.py              # Exponential decay weights + Hamilton's slot allocation
  scoring.py                # Track scoring: Last.fm popularity + setlist frequency + recency + novelty
tests/
  test_artist_resolver.py   # Title parsing, artist splitting, Spotify search, resolve_artist
  test_playlist_logic.py    # Weighting, slot allocation, track scoring, album interleaving
  test_cache.py             # KV TTL cache, play history accumulation
  test_spotify_client.py    # Variant recording filter (_is_variant_recording)
  test_ticketmaster.py      # Venue matching, Spotify ID extraction, opener enrichment
  test_setlist.py           # SetlistClient behaviour + setlist frequency in scoring
  test_lastfm.py            # LastFmClient behaviour + log-normalisation
conftest.py                 # Adds project root to sys.path for test imports
.env                        # Local secrets — never commit
.env.example                # Template with comments
```

## Configuration (.env)

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

At least one calendar source must be configured.

## Key design decisions

**Spotify auth**: PKCE flow — no client secret needed. Token stored at `~/.concert-playlist/spotify_token.json`.

**Ticketmaster opener enrichment** (`sources/ticketmaster.py`): After calendar sources are fetched, any event with fewer than 2 artists is looked up on the Ticketmaster Discovery API. The event is matched by keyword (headliner name) + date, then scored by whether the headliner appears in the attraction list (+10) and whether the venue fuzzy-matches (+5). A score ≥ 10 is required to accept the match. All non-headliner attractions become additional `Concert` objects with `source='ticketmaster'`; their `event_name` is overwritten with the headliner's calendar event name so they are treated as part of the same concert, not separate events. If Ticketmaster provides a Spotify external link for an opener, that ID is passed directly to `resolve_artist` (skipping search). Results are cached 7 days. API errors are not cached so the next run retries. Runs in both `--update`/`--dry-run` and `--status`.

**Artist resolution**: Calendar title → artist name via `artist_resolver.extract_artist_from_calendar_title` (strips "Ticket(s): " prefixes, venue/tour suffixes, etc.), then `split_artist_names` for multi-artist bills. `split_artist_names` only splits on bare commas/& when the structure unambiguously signals a list (≥2 commas, or comma + conjunction) — a single lone comma is kept intact to avoid breaking band names like "Black Country, New Road". Spotify search uses quoted phrases (`artist:"Name"`) to prevent Lucene splitting on commas. Results cached 90 days; failures cached 1 day so bug fixes take effect quickly.

**Spotify API wrappers**: Several spotipy wrapper methods pass `None` kwargs (e.g. `market=None`, `country=None`) which get serialized as the string `"None"` in query params, causing 400/403 errors. Affected methods use `sp._get()` directly with params embedded in the URL string: `_get_artist_albums` (avoids `album%2Csingle` encoding and `country=None`), `_get_popularity_batch` (avoids `market=None` 403). `_get_artist_albums` also retries with `limit //= 2` on 400 for artists with non-standard limit caps; spotipy's logger is suppressed only during the retry attempt to avoid noisy ERROR logs for handled errors.

**Variant recording filter**: `client._is_variant_recording(track_name, album_name)` skips live, acoustic, unplugged, remix, instrumental, and demo recordings before they enter the scoring pipeline. Album-level check (e.g. "Live at X", "Unplugged", "Remixes") skips the whole album. Track-level check matches both parenthetical suffixes (e.g. "Song (Live)") and dash suffixes (e.g. "Song - Acoustic", "Song - Demo") to avoid false positives on artistic titles like "Live Wire".

**Track scoring** (see `playlist_logic/scoring.py`): Fixed base weights — Last.fm 45%, setlist 25%, recency 15%, novelty 15% — normalised by the sum of weights for signals that are actually available. This means relative signal importance is preserved regardless of which APIs are configured; no separate fallback weight sets are needed. Last.fm popularity (log-normalised global play counts from `artist.getTopTracks`) is the dominant signal when available. Setlist frequency (0.0–1.0) is how often the artist plays that track across their last 10 shows within the past year, sourced from setlist.fm. When setlist data is available but a track was never played live, it is penalised (the weight enters the denominator with a zero contribution), reflecting that live omission is meaningful signal. Recency is a linear decay over 18 months. Novelty is the inverse of familiarity. Familiarity combines Spotify top-tracks API signal and local play-count history, taking the max. A per-album cap (default 6 tracks) prevents a single album from dominating; selected tracks are then interleaved across albums (newest first, round-robin) to avoid consecutive same-album runs. Spotify's `popularity` field is deprecated and returns 0 for most artists — it is not used.

**Setlist.fm integration** (`sources/setlist.py`): `SetlistClient.get_setlist_scores(artist_name)` fetches up to 10 recent shows (within the past year) via `GET /search/setlists?artistName=`. Each song appearance is counted — a song played twice in one show (e.g. as an encore) counts twice, since repeated performance is meaningful signal. Frequency = appearances / shows_analysed, and can exceed 1.0. A 1-second sleep is inserted before each API call to respect rate limits. Results cached 7 days; empty results are also cached to avoid re-hitting the API for artists with no data.

**Last.fm integration** (`sources/lastfm.py`): `LastFmClient.get_popularity_scores(artist_name)` fetches up to 50 top tracks via `artist.getTopTracks`. Play counts are log-normalised so the most-played track scores 1.0 and others scale relative to it (log scale handles the power-law distribution of streaming counts). Results cached 7 days. In `--dry-run` output, `sl` shows setlist frequency and `lf` shows Last.fm popularity score; `—` means no data for that track.

**Slot allocation** (see `playlist_logic/weighting.py`): Exponential decay with 21-day half-life. `allocate_slots` distributes a total duration budget (default 2 hours) proportionally across artists. Artists whose proportional share is below `min_tracks_per_artist × 3.5 min` are excluded. Hamilton's method distributes rounding so budgets sum to the target exactly. `select_tracks_for_artist` greedily fills each artist's time budget by score, stopping once the accumulated `duration_ms` reaches the artist's budget (rounding up to the last whole track).

**Cache** (`~/.concert-playlist/cache.db`): SQLite. Two tables — `kv_cache` (TTL-based, stores artist resolutions and discographies) and `play_history` (append-only, accumulates plays across weekly runs to improve novelty scores over time).

## Tuning knobs (config.py)

```python
concert_window_days: int = 90              # how far ahead to look for concerts
playlist_target_duration_minutes: int = 120  # target total playlist length
min_tracks_per_artist: int = 2             # min tracks worth of budget; artists below excluded
```
