"""
common.py — Shared constants and utilities for folder-cleaner.

Imported by cache.py, worker.py, formatter.py, and scanner.py.
"""

import os
import sqlite3
import time

MB = 1024 * 1024
DB_FILENAME = ".dirsize_cache.db"


def db_file_path() -> str:
    """Absolute path to the SQLite cache file (next to this script)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_FILENAME)


def norm_path(path: str) -> str:
    """Normalize to absolute path with no trailing separator.

    Uses os.path.normpath rather than rstrip(os.sep) so that Windows drive
    roots like 'C:\\' are preserved correctly — rstrip would collapse 'C:\\'
    to 'C:', which Windows treats as the CWD on that drive, not the root.
    """
    return os.path.normpath(os.path.abspath(path))


def get_inode(st) -> int | None:
    """Extract inode from a stat result, or None if unavailable/zero."""
    try:
        return st.st_ino if st.st_ino != 0 else None
    except AttributeError:
        return None


def is_cache_fresh(
    cached_mtime: float,
    current_mtime: float,
    cached_at: float,
    max_age_hours: float,
) -> bool:
    """
    Return True if a cache entry is still valid.

    max_age_hours semantics:
        0        → cache disabled entirely (always False)
        positive → expire after N hours
        negative → never auto-expire; mtime-only check
    """
    if max_age_hours == 0:
        return False
    if cached_mtime != current_mtime:
        return False
    if max_age_hours > 0:
        age_hours = (time.time() - cached_at) / 3600.0
        if age_hours > max_age_hours:
            return False
    return True


def open_db_connection(db_file: str) -> sqlite3.Connection:
    """Open a SQLite connection with standard performance PRAGMAs."""
    conn = sqlite3.connect(db_file, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")  # 32 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn
