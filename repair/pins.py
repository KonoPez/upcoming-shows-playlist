"""
Deliberate overrides for artists the automatic pipeline gets wrong.

Pins live in their own tables rather than as `artist_resolve:` cache entries,
so neither a TTL nor `--clear-cache` can quietly undo a correction.

- Artist pins map a name, as it appears on a calendar or Ticketmaster bill, to
  a Spotify ID, or to no artist at all for an act with nothing on Spotify.
  `resolve_artist` consults them before the cache or any search.
- setlist.fm pins fix the MusicBrainz ID used for an artist's setlists, keyed
  by Spotify ID because a namesake shares the name by definition.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from cache import Cache


@dataclass
class ArtistPin:
    name: str
    artist_id: Optional[str]     # None: the act has no Spotify profile
    until: Optional[date]        # None: permanent
    added_at: str


def _ensure_tables(cache: Cache) -> None:
    with cache._conn() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS artist_pins (
                name_key   TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                artist_id  TEXT,
                until      TEXT,
                added_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS setlist_mbid_pins (
                artist_id  TEXT PRIMARY KEY,
                mbid       TEXT NOT NULL,
                added_at   TEXT NOT NULL
            );
        ''')


def _name_key(name: str) -> str:
    # Same folding as the `artist_resolve:` cache key.
    return name.lower().strip()


def _pin_from_row(row) -> ArtistPin:
    return ArtistPin(
        name=row['name'],
        artist_id=row['artist_id'],
        until=date.fromisoformat(row['until']) if row['until'] else None,
        added_at=row['added_at'],
    )


def lookup_artist(cache: Cache, name: str, today: Optional[date] = None) -> Optional[ArtistPin]:
    """The live pin for `name`, or None. A pin past its `until` date is ignored."""
    _ensure_tables(cache)
    with cache._conn() as conn:
        row = conn.execute(
            'SELECT * FROM artist_pins WHERE name_key = ?', (_name_key(name),)
        ).fetchone()
    if row is None:
        return None
    pin = _pin_from_row(row)
    if pin.until is not None and pin.until < (today or date.today()):
        return None
    return pin


def pin_artist(
    cache: Cache,
    name: str,
    artist_id: Optional[str],
    until: Optional[date] = None,
) -> None:
    _ensure_tables(cache)
    with cache._conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO artist_pins (name_key, name, artist_id, until, added_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (
                _name_key(name), name.strip(), artist_id,
                until.isoformat() if until else None,
                datetime.now().isoformat(timespec='seconds'),
            ),
        )


def unpin_artist(cache: Cache, name: str) -> bool:
    _ensure_tables(cache)
    with cache._conn() as conn:
        conn.execute('DELETE FROM artist_pins WHERE name_key = ?', (_name_key(name),))
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def list_artist_pins(cache: Cache) -> list[ArtistPin]:
    """Every artist pin, expired ones included, so stale pins can be cleaned up."""
    _ensure_tables(cache)
    with cache._conn() as conn:
        rows = conn.execute('SELECT * FROM artist_pins ORDER BY name_key').fetchall()
    return [_pin_from_row(r) for r in rows]


def lookup_setlist_mbid(cache: Cache, artist_id: str) -> Optional[str]:
    _ensure_tables(cache)
    with cache._conn() as conn:
        row = conn.execute(
            'SELECT mbid FROM setlist_mbid_pins WHERE artist_id = ?', (artist_id,)
        ).fetchone()
    return row['mbid'] if row else None


def pin_setlist_mbid(cache: Cache, artist_id: str, mbid: str) -> None:
    _ensure_tables(cache)
    with cache._conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO setlist_mbid_pins (artist_id, mbid, added_at) VALUES (?, ?, ?)',
            (artist_id, mbid, datetime.now().isoformat(timespec='seconds')),
        )


def unpin_setlist_mbid(cache: Cache, artist_id: str) -> bool:
    _ensure_tables(cache)
    with cache._conn() as conn:
        conn.execute('DELETE FROM setlist_mbid_pins WHERE artist_id = ?', (artist_id,))
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def list_setlist_mbid_pins(cache: Cache) -> dict[str, str]:
    _ensure_tables(cache)
    with cache._conn() as conn:
        rows = conn.execute('SELECT artist_id, mbid FROM setlist_mbid_pins').fetchall()
    return {r['artist_id']: r['mbid'] for r in rows}
