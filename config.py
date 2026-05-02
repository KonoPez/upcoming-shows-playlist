import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_DIR = Path.home() / '.concert-playlist'
APP_DIR.mkdir(exist_ok=True)


def _env_str(key: str, default: str = '') -> str:
    """Read an env var, stripping inline comments and surrounding whitespace."""
    raw = os.getenv(key, default) or default
    return raw.split('#')[0].strip()


@dataclass
class Config:
    # Spotify
    spotify_client_id: str = field(default_factory=lambda: _env_str('SPOTIFY_CLIENT_ID'))
    spotify_redirect_uri: str = field(
        default_factory=lambda: _env_str('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:8080')
    )
    playlist_id: str = field(default_factory=lambda: _env_str('PLAYLIST_ID'))
    playlist_name: str = field(default_factory=lambda: _env_str('PLAYLIST_NAME', 'Concert Prep'))

    # Apple Calendar
    apple_username: str = field(default_factory=lambda: _env_str('APPLE_CALENDAR_USERNAME'))
    apple_app_password: str = field(default_factory=lambda: _env_str('APPLE_CALENDAR_APP_PASSWORD'))

    # Google Calendar
    google_calendar_ics_url: str = field(
        default_factory=lambda: _env_str('GOOGLE_CALENDAR_ICS_URL')
    )

    # Ticketmaster (optional — used to find opening acts)
    ticketmaster_api_key: str = field(
        default_factory=lambda: _env_str('TICKETMASTER_API_KEY')
    )

    # Setlist.fm (optional — boosts tracks that regularly appear in live setlists)
    setlist_fm_api_key: str = field(
        default_factory=lambda: _env_str('SETLIST_FM_API_KEY')
    )

    # Playlist tuning
    concert_window_days: int = 90
    playlist_target_duration_minutes: int = 120
    min_tracks_per_artist: int = 2

    @property
    def spotify_token_path(self) -> str:
        return str(APP_DIR / 'spotify_token.json')

    def validate_required(self) -> None:
        missing = []
        if not self.spotify_client_id:
            missing.append('SPOTIFY_CLIENT_ID')
        if not (self.apple_username and self.apple_app_password) \
                and not self.google_calendar_ics_url:
            missing.append(
                'at least one calendar source '
                '(APPLE_CALENDAR_USERNAME + APPLE_CALENDAR_APP_PASSWORD, '
                'or GOOGLE_CALENDAR_ICS_URL)'
            )
        if missing:
            raise ValueError(
                'Missing required configuration:\n'
                + '\n'.join(f'  {m}' for m in missing)
                + '\n\nSet these in .env or run: python main.py --setup'
            )


config = Config()
