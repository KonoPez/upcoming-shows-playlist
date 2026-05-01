"""
SQLite-backed cache used for two distinct purposes:

1. Key-value cache with TTL — stores Ticketmaster/Spotify API responses so
   repeated runs don't re-fetch identical data (artist discographies, artist
   name → Spotify ID resolutions, etc.).

2. Play history accumulation — Spotify's recently-played endpoint only returns
   the last 50 plays. We accumulate all plays we've ever seen here so the
   novelty/familiarity scores improve over time with each weekly run.
"""

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

APP_DIR = Path.home() / '.concert-playlist'
CACHE_DB = APP_DIR / 'cache.db'


class Cache:
    def __init__(self, db_path: Path = CACHE_DB):
        db_path.parent.mkdir(exist_ok=True)
        self.db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS kv_cache (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    expires_at  REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS play_history (
                    track_id    TEXT NOT NULL,
                    artist_id   TEXT NOT NULL,
                    played_at   TEXT NOT NULL,
                    PRIMARY KEY (track_id, played_at)
                );

                CREATE INDEX IF NOT EXISTS ph_artist ON play_history (artist_id);
                CREATE INDEX IF NOT EXISTS ph_track  ON play_history (track_id);
            ''')

    # ── Key-value cache ──────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT value, expires_at FROM kv_cache WHERE key = ?', (key,)
            ).fetchone()
            if row and row['expires_at'] > time.time():
                try:
                    return json.loads(row['value'])
                except json.JSONDecodeError:
                    return None
        return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)',
                (key, json.dumps(value), time.time() + ttl_seconds),
            )

    def delete(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute('DELETE FROM kv_cache WHERE key = ?', (key,))

    def clear_all(self) -> dict[str, int]:
        """Delete all kv_cache entries. Play history is preserved."""
        with self._conn() as conn:
            conn.execute('DELETE FROM kv_cache')
            n = conn.execute('SELECT changes()').fetchone()[0]
        return {'kv_cache': n}

    def clear_expired(self) -> int:
        with self._conn() as conn:
            conn.execute('DELETE FROM kv_cache WHERE expires_at <= ?', (time.time(),))
            return conn.execute('SELECT changes()').fetchone()[0]

    # ── Play history accumulation ────────────────────────────────────────────

    def record_plays(self, plays: list[dict]) -> int:
        """
        Upsert play history entries.
        Each entry must have: track_id, artist_id, played_at (ISO-8601 string).
        Returns the number of newly inserted rows.
        """
        inserted = 0
        with self._conn() as conn:
            for play in plays:
                try:
                    conn.execute(
                        'INSERT OR IGNORE INTO play_history (track_id, artist_id, played_at) '
                        'VALUES (?, ?, ?)',
                        (play['track_id'], play['artist_id'], play['played_at']),
                    )
                    if conn.execute('SELECT changes()').fetchone()[0]:
                        inserted += 1
                except sqlite3.Error as e:
                    logger.debug(f'Skipping play record: {e}')
        return inserted

    def get_play_counts(self, artist_id: str) -> dict[str, int]:
        """Return {track_id: lifetime_play_count} for every track by a given artist."""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT track_id, COUNT(*) as cnt FROM play_history '
                'WHERE artist_id = ? GROUP BY track_id',
                (artist_id,),
            ).fetchall()
        return {row['track_id']: row['cnt'] for row in rows}
