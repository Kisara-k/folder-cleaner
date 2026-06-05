"""
worker.py — Multiprocessing worker for subtree size computation.

Each worker receives a root directory path and computes the full recursive
size + file count using iterative DFS + os.scandir. Results are sent back
to the main process via a multiprocessing.Queue for centralized cache writes.
"""

import os
from typing import Optional

from common import get_inode, is_cache_fresh, norm_path, open_db_connection

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

def compute_subtree(args: tuple) -> list[dict]:
    """
    Compute recursive size + file count for a directory subtree.
    Returns a list of result dicts (one per directory encountered).

    args: (root_path, current_mtime, cache_db_path, max_age_hours)
    Each result dict:
        {
          "path": str,
          "size_bytes": int,
          "file_count": int,
          "mtime": float,
          "inode": int|None,
          "is_dir": bool,
          "is_junk": bool,
        }
    """
    import sqlite3

    root_path, root_mtime, cache_db_path, max_age_hours = args

    results: list[dict] = []

    def _get_cached(conn: sqlite3.Connection, path: str, mtime: float) -> Optional[tuple[int, int]]:
        row = conn.execute(
            "SELECT size_bytes, file_count, mtime, cached_at FROM dir_cache WHERE path=?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        size_bytes, file_count, cached_mtime, cached_at = row
        if not is_cache_fresh(cached_mtime, mtime, cached_at, max_age_hours):
            return None
        return (size_bytes, file_count)

    conn = open_db_connection(cache_db_path)

    # Seed root stat for junction guard
    try:
        root_st = os.stat(root_path)
        root_inode = get_inode(root_st)
        root_dev = getattr(root_st, "st_dev", 0)
    except OSError:
        root_inode = None
        root_dev = 0

    # visited tracks (st_dev, inode) to detect NTFS junction cycles.
    # Falls back to norm_path when inode is unavailable (st_ino == 0).
    visited: set = set()

    # Stack entries: (path, mtime, inode, st_dev, is_junk)
    dir_stack: list[tuple[str, float, Optional[int], int, bool]] = [
        (root_path, root_mtime, root_inode, root_dev, False)
    ]
    dir_nodes: list[dict] = []

    while dir_stack:
        path, mtime, inode, st_dev, is_junk = dir_stack.pop()
        path = norm_path(path)

        # Junction / hard-link cycle guard
        visit_key = (st_dev, inode) if inode else path
        if visit_key in visited:
            continue
        visited.add(visit_key)

        # SKIP_PATHS: treat matched absolute paths like junk (shallow scan only)
        if path in SKIP_PATHS:
            is_junk = True

        node_idx = len(dir_nodes)
        dir_nodes.append({
            "path": path,
            "mtime": mtime,
            "inode": inode,
            "is_junk": is_junk,
            "size_bytes": 0,
            "file_count": 0,
            "direct_file_bytes": 0,
            "direct_file_count": 0,
            "child_indices": [],
            "_cached": False,
        })

        cached = _get_cached(conn, path, mtime)
        if cached is not None:
            dir_nodes[node_idx]["size_bytes"] = cached[0]
            dir_nodes[node_idx]["file_count"] = cached[1]
            dir_nodes[node_idx]["_cached"] = True
            continue

        if is_junk:
            _shallow_scan(path, dir_nodes[node_idx])
            continue

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
                        dir_nodes[node_idx]["direct_file_bytes"] += st.st_size
                        dir_nodes[node_idx]["direct_file_count"] += 1
                    elif entry.is_dir(follow_symlinks=False):
                        child_path = norm_path(entry.path)
                        child_is_junk = entry.name in SKIP_DIRS or child_path in SKIP_PATHS
                        child_idx = len(dir_nodes)
                        dir_nodes[node_idx]["child_indices"].append(child_idx)
                        dir_stack.append((
                            child_path,
                            st.st_mtime,
                            get_inode(st),
                            getattr(st, "st_dev", 0),
                            child_is_junk,
                        ))
        except OSError:
            pass

    # Phase 2: bottom-up aggregation (reverse DFS order)
    for node_idx in range(len(dir_nodes) - 1, -1, -1):
        node = dir_nodes[node_idx]
        if node["_cached"] or node["is_junk"]:
            results.append(_node_to_result(node))
            continue

        total_bytes = node["direct_file_bytes"]
        total_files = node["direct_file_count"]
        for child_idx in node["child_indices"]:
            child = dir_nodes[child_idx]
            total_bytes += child["size_bytes"]
            total_files += child["file_count"]

        node["size_bytes"] = total_bytes
        node["file_count"] = total_files
        results.append(_node_to_result(node))

    conn.close()
    return results


def _shallow_scan(path: str, node: dict) -> None:
    """Scan only one level deep for junk/skip-path dirs."""
    total_bytes = 0
    total_files = 0
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
    except OSError:
        pass
    node["size_bytes"] = total_bytes
    node["file_count"] = total_files


def _node_to_result(node: dict) -> dict:
    return {
        "path": node["path"],
        "size_bytes": node["size_bytes"],
        "file_count": node["file_count"],
        "mtime": node["mtime"],
        "inode": node["inode"],
        "is_dir": True,
        "is_junk": node["is_junk"],
    }


# ------------------------------------------------------------------
# Convenience wrapper for in-process use
# ------------------------------------------------------------------

def scan_subtree_inprocess(
    root_path: str,
    skip_dirs: frozenset[str],
    skip_paths: frozenset[str],
    cache_db_path: str,
    max_age_hours: float,
) -> list[dict]:
    """Same as compute_subtree but runs in the calling process."""
    global SKIP_DIRS, SKIP_PATHS
    SKIP_DIRS = skip_dirs
    SKIP_PATHS = skip_paths
    try:
        mtime = os.stat(root_path).st_mtime
    except OSError:
        mtime = 0.0
    return compute_subtree((root_path, mtime, cache_db_path, max_age_hours))
