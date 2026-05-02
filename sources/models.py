from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional


@dataclass
class Track:
    id: str
    name: str
    release_date: str
    release_date_precision: str
    duration_ms: int
    album_id: str
    album_name: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'Track':
        return cls(
            id=d['id'],
            name=d['name'],
            release_date=d.get('release_date', ''),
            release_date_precision=d.get('release_date_precision', 'year'),
            duration_ms=d.get('duration_ms', 0),
            album_id=d.get('album_id', ''),
            album_name=d.get('album_name', ''),
        )


@dataclass
class Concert:
    event_name: str
    artist_name: str
    event_date: date
    venue: str
    source: str
    tm_spotify_id: Optional[str] = None  # Spotify artist ID if known from source metadata
    is_opener: bool = False              # True for supporting acts found via Ticketmaster
