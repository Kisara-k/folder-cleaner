"""
cache.py — SQLite cache layer for folder-cleaner.

All paths stored as normalized absolute strings (no trailing slashes).
Schema version: 1
"""

import os
import sqlite3
import time
from typing import Optional

from common import db_file_path, is_cache_fresh, norm_path, open_db_connection

SCHEMA_VERSION = "1"

_CREATE_DIR_CACHE = """
CREATE TABLE IF NOT EXISTS dir_cache (
    path        TEXT    PRIMARY KEY NOT NULL,
    size_bytes  INTEGER NOT NULL,
    file_count  INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    inode       INTEGER,
    is_dir      INTEGER NOT NULL,
    cached_at   REAL    NOT NULL,
    is_junk     INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_path ON dir_cache (path);
"""


def open_cache(invalidate: bool = False, max_age_hours: float = 0) -> "CacheDB":
    """Open (or create) the cache database. Returns a CacheDB instance."""
    db_file = db_file_path()
    try:
        conn = open_db_connection(db_file)
        _init_schema(conn)
        db = CacheDB(conn, db_file, max_age_hours)
        if invalidate:
            db.clear()
        return db
    except sqlite3.DatabaseError:
        print(f"[WARN] Cache DB corrupted at {db_file}. Recreating...")
        try:
            os.remove(db_file)
        except OSError:
            pass
        conn = open_db_connection(db_file)
        _init_schema(conn)
        return CacheDB(conn, db_file, max_age_hours)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_DIR_CACHE)
    conn.execute(_CREATE_META)
    conn.execute(_CREATE_INDEX)
    row = conn.execute("SELECT value FROM cache_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    elif row[0] != SCHEMA_VERSION:
        # Schema mismatch — wipe and reinit
        conn.execute("DROP TABLE IF EXISTS dir_cache")
        conn.execute("DROP TABLE IF EXISTS cache_meta")
        conn.execute(_CREATE_DIR_CACHE)
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_INDEX)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()


class CacheDB:
    """Thread-unsafe, process-local cache interface."""

    def __init__(self, conn: sqlite3.Connection, db_file: str, max_age_hours: float):
        self._conn = conn
        self._db_file = db_file
        self._max_age_hours = max_age_hours
        self._hits = 0
        self._misses = 0
        self._batch: list[tuple] = []
        self._pre_run_paths: set[str] = set()
        self._BATCH_SIZE = 500

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_pre_run_snapshot(self) -> None:
        """Record which paths are cached right now, before any scans in this run.

        Only entries present in this snapshot will be counted as cache hits.
        Entries written by an earlier target's scan in the same run are treated
        as fresh computes, so child-dir reports don't falsely claim cache reuse.
        """
        rows = self._conn.execute("SELECT path FROM dir_cache").fetchall()
        self._pre_run_paths = {r[0] for r in rows}

    def reset_scan_counters(self) -> None:
        """Reset hit/miss counters before scanning each target directory."""
        self._hits = 0
        self._misses = 0

    def lookup(
        self,
        path: str,
        current_mtime: float,
    ) -> Optional[tuple[int, int, bool]]:
        """
        Return (size_bytes, file_count, is_junk) if cache is valid, else None.
        """
        path = norm_path(path)
        row = self._conn.execute(
            "SELECT size_bytes, file_count, mtime, cached_at, is_junk FROM dir_cache WHERE path=?",
            (path,),
        ).fetchone()
        if row is None:
            self._misses += 1
            return None
        size_bytes, file_count, cached_mtime, cached_at, is_junk = row
        if not is_cache_fresh(cached_mtime, current_mtime, cached_at, self._max_age_hours):
            self._misses += 1
            return None
        # Only count as a hit if this entry existed before the current run started.
        # Entries populated by an earlier target's scan this session are "fresh
        # computes" from the user's perspective, not reused past-cache.
        if path in self._pre_run_paths:
            self._hits += 1
        else:
            self._misses += 1
        return (size_bytes, file_count, bool(is_junk))

    def store(
        self,
        path: str,
        size_bytes: int,
        file_count: int,
        mtime: float,
        inode: Optional[int],
        is_dir: bool,
        is_junk: bool = False,
    ) -> None:
        """Queue an entry for batch insertion."""
        path = norm_path(path)
        now = time.time()
        self._batch.append((path, size_bytes, file_count, mtime, inode, int(is_dir), now, int(is_junk)))
        if len(self._batch) >= self._BATCH_SIZE:
            self._flush()

    def flush(self) -> None:
        """Public flush — called at end of scan."""
        self._flush()
        self._conn.commit()

    def clear(self) -> None:
        """Wipe all cached entries."""
        self._conn.execute("DELETE FROM dir_cache")
        self._conn.commit()
        print("[INFO] Cache cleared.")

    def update_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_cached_at(self, path: str) -> Optional[float]:
        """Return cached_at timestamp for a path, or None if cache is disabled."""
        if self._max_age_hours == 0:
            return None
        path = norm_path(path)
        row = self._conn.execute(
            "SELECT cached_at FROM dir_cache WHERE path=?", (path,)
        ).fetchone()
        return row[0] if row else None

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def close(self) -> None:
        self._flush()
        self._conn.commit()
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        if not self._batch:
            return
        self._conn.executemany(
            """INSERT OR REPLACE INTO dir_cache
               (path, size_bytes, file_count, mtime, inode, is_dir, cached_at, is_junk)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            self._batch,
        )
        self._conn.commit()
        self._batch.clear()
