"""
Spotify authentication using Authorization Code + PKCE.

Spotify explicitly recommends PKCE for CLI and desktop apps because the client
secret cannot be stored securely on a user's machine. PKCE uses a
cryptographic code challenge instead, so no secret is needed at all.

- During setup (open_browser=True):  opens a browser for the consent screen.
- During cron runs (open_browser=False): refreshes silently using the stored
  refresh token. Raises RuntimeError if no valid token exists.
"""

import logging
from pathlib import Path

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

logger = logging.getLogger(__name__)

SCOPES = ' '.join([
    'playlist-read-private',
    'playlist-modify-private',
    'playlist-modify-public',
    'user-top-read',
    'user-read-recently-played',
    'user-library-read',
    'user-read-private',
])


def get_spotify_client(
    client_id: str,
    redirect_uri: str,
    token_path: str,
    open_browser: bool = False,
) -> spotipy.Spotify:
    cache_handler = CacheFileHandler(
        cache_path=str(Path(token_path).expanduser())
    )
    auth_manager = SpotifyPKCE(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        cache_handler=cache_handler,
        open_browser=open_browser,
    )
    sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=30)

    try:
        user = sp.current_user()
        logger.info(
            f'Spotify authenticated as: '
            f'{user.get("display_name") or user.get("id")}'
        )
    except Exception as e:
        raise RuntimeError(
            f'Spotify authentication failed: {e}\n'
            'Run "python main.py --setup" to authenticate.'
        ) from e

    return sp
