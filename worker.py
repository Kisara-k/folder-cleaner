"""
worker.py — Multiprocessing worker for subtree size computation.

Each worker receives a root directory path and computes the full recursive
size + file count using iterative DFS + os.scandir. No caching.
"""

import os
from typing import Optional

from common import get_inode, norm_path

SKIP_DIRS: frozenset[str] = frozenset()   # directory names to shallow-scan (set by _pool_init)
SKIP_PATHS: frozenset[str] = frozenset()  # absolute paths to shallow-scan (set by _pool_init)


# ------------------------------------------------------------------
# Worker initializer — called once per process in the Pool
# ------------------------------------------------------------------

def _pool_init(skip_dirs: frozenset[str], skip_paths: frozenset[str]) -> None:
    global SKIP_DIRS, SKIP_PATHS
    SKIP_DIRS = skip_dirs
    SKIP_PATHS = skip_paths


# ------------------------------------------------------------------
# Core subtree computation (runs inside worker process)
# ------------------------------------------------------------------

def compute_subtree(root_path: str) -> tuple[int, int]:
    """
    Compute recursive size + file count for a directory subtree.
    Returns (total_bytes, total_file_count).
    """
    root_path = norm_path(root_path)

    # Seed root stat for junction guard
    try:
        root_st = os.stat(root_path)
        root_inode = get_inode(root_st)
        root_dev = getattr(root_st, "st_dev", 0)
    except OSError:
        return 0, 0

    # visited tracks (st_dev, inode) to detect NTFS junction cycles.
    visited: set = set()

    # Stack entries: (path, inode, st_dev, is_junk)
    dir_stack: list[tuple[str, Optional[int], int, bool]] = [
        (root_path, root_inode, root_dev, False)
    ]

    total_bytes = 0
    total_files = 0

    while dir_stack:
        path, inode, st_dev, is_junk = dir_stack.pop()
        path = norm_path(path)

        # Junction / hard-link cycle guard
        visit_key = (st_dev, inode) if inode else path
        if visit_key in visited:
            continue
        visited.add(visit_key)

        # SKIP_PATHS: treat matched absolute paths as junk (shallow scan only)
        if path in SKIP_PATHS:
            is_junk = True

        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    if entry.is_file(follow_symlinks=False):
                        total_bytes += st.st_size
                        total_files += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if is_junk:
                            # For junk dirs, do shallow scan only (no recursion)
                            total_bytes += st.st_size
                        else:
                            # For normal dirs, recurse
                            child_path = norm_path(entry.path)
                            child_is_junk = entry.name in SKIP_DIRS or child_path in SKIP_PATHS
                            dir_stack.append((
                                child_path,
                                get_inode(st),
                                getattr(st, "st_dev", 0),
                                child_is_junk,
                            ))
        except OSError:
            pass

    return total_bytes, total_files
