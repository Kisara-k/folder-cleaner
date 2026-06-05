"""
common.py — Shared constants and utilities for folder-cleaner.
"""

import os

MB = 1024 * 1024


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
