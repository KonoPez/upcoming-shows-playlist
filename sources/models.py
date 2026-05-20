import re
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Optional

_VARIANT_KEYWORDS = r'(?:live|acoustic|unplugged|remix|instrumental|demo|a\s*cappella|acapella)'


def normalize_track_name(name: str) -> str:
    """
    Strip variant suffixes and lowercase a track name for use as a lookup key.

    "Dancers - Live at Bush Hall" → "dancers"
    "Song (Live at Glastonbury)"  → "song"
    "Song (Acoustic Version)"     → "song"
    "Concorde"                    → "concorde"

    Used both when grouping tracks by song identity (deduplication) and when
    looking up setlist/Last.fm scores, so that live-only releases match the
    canonical song name used by those external services.
    """
    stripped = re.sub(rf'\s*\(.*\b{_VARIANT_KEYWORDS}\b.*\).*$', '', name, flags=re.IGNORECASE)
    stripped = re.sub(rf'\s*[-–]\s*{_VARIANT_KEYWORDS}\b.*$', '', stripped, flags=re.IGNORECASE)
    return stripped.strip().lower()


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

    def days_until(self, today: date) -> int:
        return (self.event_date - today).days


@dataclass
class Artist:
    spotify_id: str
    name: str
    concerts: list[Concert] = field(default_factory=list)
    setlist_scores: Optional[dict[str, float]] = None
    lastfm_scores: Optional[dict[str, float]] = None
    selected_tracks: list[Track] = field(default_factory=list)
