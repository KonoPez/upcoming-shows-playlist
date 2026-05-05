# Concert Playlist

Two Spotify playlists:

**Concert Prep** — reads your upcoming concerts from Apple Calendar or Google Calendar and builds a playlist to get you ready for each show. Tracks are scored and weighted so shows happening sooner get more weight, and songs you already know well are pushed toward the back. Setlist.fm data allows the playlist to be based as much as possible off real recent setlists, so listening to this playlist for a few days should expose you to pretty much everything you'll hear at the show. Opening acts are included, but the playlist is weighted such that the headliner gets more of the focus. 

**Concert Discoveries** — finds upcoming concerts near you on Ticketmaster and surfaces artists you might actually enjoy. Artists are scored by how familiar they already are to you (based on your Spotify listening history) and how popular they are globally. Artists you already have tickets to are filtered out automatically.

Both playlists can be configured to update on a schedule via cron.

---

## Requirements

- Python 3.9+
- A [Spotify account](https://spotify.com) and a free [Spotify developer app](https://developer.spotify.com/dashboard)
- At least one calendar source connected (for Concert Prep), or a Ticketmaster API key (for Concert Discoveries)

---

## Setup

```bash
pip install -r requirements.txt
python main.py --setup
```

The setup wizard walks you through Spotify authentication and creates the playlist. When it's done, optionally run `python setup_cron.py` to schedule automatic updates.

---

## Usage

```bash
python main.py --update             # update Concert Prep playlist
python main.py --dry-run            # preview Concert Prep without writing to Spotify
python main.py --status             # print upcoming concerts, no writes

python main.py --discover           # update Concert Discoveries playlist
python main.py --discover-dry-run   # preview Concert Discoveries without writing to Spotify

python main.py --cache-status       # show cache stats and last run info
python main.py --clear-cache        # clear cached data (preserves play history)
```

---

## API keys and your data

This app runs entirely on your own machine. Credentials are stored only in a local `.env` file and `~/.concert-playlist/` — they are never sent anywhere other than directly to the respective service APIs. The code is open source and you can verify this yourself. That said, review the code before entering any passwords, and use app-specific passwords (not your main account password) wherever possible.

### Spotify (required)

Create a free app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard). You only need the client ID — this app uses the PKCE auth flow, which requires no client secret. Set the redirect URI to `http://127.0.0.1:8080` in your Spotify app settings.

### Calendar (required for Concert Prep)

At least one of the following:

**Apple Calendar / iCloud** — provide your iCloud email and an [app-specific password](https://appleid.apple.com) (not your Apple ID password). Most ticketing services (Ticketmaster, Dice, AXS) send a calendar invite when you buy tickets — accept it and your concerts appear automatically.

**Google Calendar** — no OAuth or Google Cloud project needed. Go to Google Calendar → Settings → your calendar → "Secret address in iCal format" and paste the URL.

### Ticketmaster (optional for Concert Prep, required for Concert Discoveries)

Free key at [developer.ticketmaster.com](https://developer.ticketmaster.com). Used to find opening acts not listed in your calendar (Concert Prep) and to discover local concerts (Concert Discoveries). The free tier allows 5,000 requests/day, which is well within what this app uses.

### Setlist.fm (optional)

Free key at [setlist.fm/settings/apps](https://www.setlist.fm/settings/apps). When set, tracks that an artist has regularly played at recent live shows get a score boost — useful for surfacing likely setlist songs over deep cuts.

### Last.fm (optional)

Free key at [last.fm/api/account/create](https://www.last.fm/api/account/create). When set, tracks are scored by global play count popularity, which becomes an influential ranking signal. Also used to score artist popularity for Concert Discoveries.