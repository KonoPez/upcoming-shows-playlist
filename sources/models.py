from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class Concert:
    event_name: str
    artist_name: str
    event_date: date
    venue: str
    source: str
    tm_spotify_id: Optional[str] = None  # Spotify artist ID if known from source metadata
    is_opener: bool = False              # True for supporting acts found via Ticketmaster
