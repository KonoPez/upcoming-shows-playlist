"""
SQLite-backed cache used for two distinct purposes:

1. Key-value cache with TTL — stores Ticketmaster/Spotify API responses so
   repeated runs don't re-fetch identical data (artist discographies, artist
   name → Spotify ID resolutions, etc.).

2. Play history accumulation — Spotify's recently-played endpoint only returns
   the last 50 plays. We accumulate all plays we've ever seen here so the
   novelty/familiarity scores improve over time with each weekly run.
"""

import datetime
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

                CREATE TABLE IF NOT EXISTS run_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at   TEXT NOT NULL,
                    trigger  TEXT NOT NULL CHECK (trigger IN ('manual', 'cron'))
                );

                CREATE TABLE IF NOT EXISTS discovery_blocklist (
                    artist_id   TEXT PRIMARY KEY,
                    artist_name TEXT NOT NULL,
                    added_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_concerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist_name TEXT NOT NULL,
                    event_date  TEXT NOT NULL,
                    venue       TEXT NOT NULL DEFAULT '',
                    event_name  TEXT NOT NULL DEFAULT '',
                    is_opener   INTEGER NOT NULL DEFAULT 0,
                    added_at    TEXT NOT NULL
                );
            ''')

            # Migration: add is_opener to manual_concerts tables created before
            # the column existed. CREATE TABLE IF NOT EXISTS won't add it.
            cols = [r['name'] for r in conn.execute('PRAGMA table_info(manual_concerts)')]
            if 'is_opener' not in cols:
                conn.execute(
                    'ALTER TABLE manual_concerts ADD COLUMN is_opener INTEGER NOT NULL DEFAULT 0'
                )

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
        with self._conn() as conn:
            before = conn.total_changes
            for play in plays:
                try:
                    conn.execute(
                        'INSERT OR IGNORE INTO play_history (track_id, artist_id, played_at) '
                        'VALUES (?, ?, ?)',
                        (play['track_id'], play['artist_id'], play['played_at']),
                    )
                except sqlite3.Error as e:
                    logger.debug(f'Skipping play record: {e}')
            inserted = conn.total_changes - before
        return inserted

    def get_artist_play_counts(self) -> dict[str, int]:
        """Return {artist_id: lifetime_play_count} for all artists in play history."""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT artist_id, COUNT(*) as cnt FROM play_history GROUP BY artist_id'
            ).fetchall()
        return {row['artist_id']: row['cnt'] for row in rows}

    def get_play_counts(self, artist_id: str) -> dict[str, int]:
        """Return {track_id: lifetime_play_count} for every track by a given artist."""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT track_id, COUNT(*) as cnt FROM play_history '
                'WHERE artist_id = ? GROUP BY track_id',
                (artist_id,),
            ).fetchall()
        return {row['track_id']: row['cnt'] for row in rows}

    # ── Run log ──────────────────────────────────────────────────────────────

    def record_run(self, trigger: str) -> None:
        """Record a completed playlist update. trigger must be 'manual' or 'cron'."""
        run_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO run_log (run_at, trigger) VALUES (?, ?)',
                (run_at, trigger),
            )

    def get_last_run(self) -> 'Optional[dict]':
        """Return {'run_at': ..., 'trigger': ...} for the most recent run, or None."""
        with self._conn() as conn:
            row = conn.execute(
                'SELECT run_at, trigger FROM run_log ORDER BY id DESC LIMIT 1'
            ).fetchone()
        if row:
            return {'run_at': row['run_at'], 'trigger': row['trigger']}
        return None

    # ── Discovery blocklist ──────────────────────────────────────────────────

    def add_blocked_artist(self, artist_id: str, artist_name: str) -> None:
        """Add an artist to the discovery blocklist. Idempotent."""
        added_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO discovery_blocklist (artist_id, artist_name, added_at) '
                'VALUES (?, ?, ?)',
                (artist_id, artist_name, added_at),
            )

    def remove_blocked_artist(self, artist_id: str) -> bool:
        """Remove an artist from the blocklist. Returns True if a row was deleted."""
        with self._conn() as conn:
            conn.execute('DELETE FROM discovery_blocklist WHERE artist_id = ?', (artist_id,))
            return conn.execute('SELECT changes()').fetchone()[0] > 0

    def get_blocked_artists(self) -> dict[str, str]:
        """Return {artist_id: artist_name} for every blocked artist."""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT artist_id, artist_name FROM discovery_blocklist'
            ).fetchall()
        return {row['artist_id']: row['artist_name'] for row in rows}

    # ── Manual concerts ──────────────────────────────────────────────────────

    def add_manual_concert(
        self,
        artist_name: str,
        event_date: str,
        venue: str = '',
        event_name: str = '',
        is_opener: bool = False,
    ) -> int:
        """
        Add a manual concert. event_date must be a YYYY-MM-DD string.
        Set is_opener=True for a supporting act (headliner is the default).
        Returns the new row ID.
        """
        added_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO manual_concerts '
                '(artist_name, event_date, venue, event_name, is_opener, added_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (artist_name, event_date, venue, event_name, int(is_opener), added_at),
            )
            return conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    def remove_manual_concert(self, concert_id: int) -> bool:
        """Remove a manual concert by ID. Returns True if a row was deleted."""
        with self._conn() as conn:
            conn.execute('DELETE FROM manual_concerts WHERE id = ?', (concert_id,))
            return conn.execute('SELECT changes()').fetchone()[0] > 0

    def remove_manual_concerts(self, ids: list[int]) -> int:
        """Batch-delete manual concerts by ID list. Returns count deleted."""
        if not ids:
            return 0
        with self._conn() as conn:
            placeholders = ','.join('?' * len(ids))
            conn.execute(f'DELETE FROM manual_concerts WHERE id IN ({placeholders})', ids)
            return conn.execute('SELECT changes()').fetchone()[0]

    def get_manual_concerts(self) -> list[dict]:
        """Return all manual concerts ordered by date."""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id, artist_name, event_date, venue, event_name, is_opener, added_at '
                'FROM manual_concerts ORDER BY event_date'
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['is_opener'] = bool(entry['is_opener'])
            result.append(entry)
        return result

    def get_summary(self) -> dict:
        """Return aggregate stats across all three tables for display."""
        now = time.time()
        with self._conn() as conn:
            kv_total = conn.execute('SELECT COUNT(*) FROM kv_cache').fetchone()[0]
            kv_live  = conn.execute('SELECT COUNT(*) FROM kv_cache WHERE expires_at > ?', (now,)).fetchone()[0]

            ph_plays   = conn.execute('SELECT COUNT(*) FROM play_history').fetchone()[0]
            ph_tracks  = conn.execute('SELECT COUNT(DISTINCT track_id) FROM play_history').fetchone()[0]
            ph_artists = conn.execute('SELECT COUNT(DISTINCT artist_id) FROM play_history').fetchone()[0]

            run_total = conn.execute('SELECT COUNT(*) FROM run_log').fetchone()[0]

        return {
            'kv_total': kv_total,
            'kv_live': kv_live,
            'kv_expired': kv_total - kv_live,
            'ph_plays': ph_plays,
            'ph_tracks': ph_tracks,
            'ph_artists': ph_artists,
            'run_total': run_total,
            'last_run': self.get_last_run(),
        }
