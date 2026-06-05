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

    args: (root_path, current_mtime, cache_db_path, max_age_hours, force_recompute=False)
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

    # Handle both old (4-tuple) and new (5-tuple) argument formats for compatibility
    if len(args) == 4:
        root_path, root_mtime, cache_db_path, max_age_hours = args
        force_recompute = False
    else:
        root_path, root_mtime, cache_db_path, max_age_hours, force_recompute = args

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

        # Skip cache for root if force_recompute is True
        skip_cache = force_recompute and (path == norm_path(root_path))
        if not skip_cache:
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
    """
    Scan junk/skip-path dirs recursively but don't descend for tree expansion.
    Uses system command for speed: PowerShell on Windows, du on Unix.
    Falls back to Python traversal if system command fails.
    """
    # Try fast system command approach first
    total_bytes = _get_dir_size_fast(path)
    if total_bytes >= 0:
        node["size_bytes"] = total_bytes
        node["file_count"] = _count_files_recursive(path)
        return

    # Fallback: full Python traversal (needed if system commands unavailable)
    total_bytes = 0
    total_files = 0
    stack = [path]
    visited = set()

    while stack:
        current = stack.pop()
        try:
            current_st = os.stat(current)
            visit_key = (getattr(current_st, "st_dev", 0), getattr(current_st, "st_ino", 0))
            if visit_key in visited:
                continue
            visited.add(visit_key)
        except OSError:
            continue

        try:
            with os.scandir(current) as it:
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
                        stack.append(entry.path)
        except OSError:
            pass

    node["size_bytes"] = total_bytes
    node["file_count"] = total_files


def _get_dir_size_fast(path: str) -> int:
    """
    Get total directory size using system command.
    Returns size in bytes, or -1 if system command unavailable/failed.
    PowerShell on Windows, du on Unix/Linux.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            # Windows: use PowerShell with proper escaping
            # Handle empty directories: Measure-Object returns $null, so default to 0
            ps_cmd = (
                f"$sum = (Get-ChildItem -Path '{path}' -Recurse -Force -ErrorAction SilentlyContinue | "
                f"Measure-Object -Property Length -Sum).Sum; "
                f"if ($null -eq $sum) {{ 0 }} else {{ $sum }}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                out = result.stdout.strip()
                if out:
                    return int(out)
        else:
            # Unix/Linux: use du -sb (non-recursive total size)
            result = subprocess.run(
                ["du", "-sb", path],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                parts = result.stdout.split()
                if parts:
                    return int(parts[0])
    except (subprocess.TimeoutExpired, ValueError, OSError, IndexError):
        pass

    return -1  # Fallback to Python traversal


def _count_files_recursive(path: str) -> int:
    """Count all files recursively (used with fast size getter)."""
    count = 0
    stack = [path]
    visited = set()

    while stack:
        current = stack.pop()
        try:
            current_st = os.stat(current)
            visit_key = (getattr(current_st, "st_dev", 0), getattr(current_st, "st_ino", 0))
            if visit_key in visited:
                continue
            visited.add(visit_key)
        except OSError:
            continue

        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    try:
                        entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    if entry.is_file(follow_symlinks=False):
                        count += 1
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            pass

    return count


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
    force_recompute: bool = False,
) -> list[dict]:
    """Same as compute_subtree but runs in the calling process."""
    global SKIP_DIRS, SKIP_PATHS
    SKIP_DIRS = skip_dirs
    SKIP_PATHS = skip_paths
    try:
        mtime = os.stat(root_path).st_mtime
    except OSError:
        mtime = 0.0
    return compute_subtree((root_path, mtime, cache_db_path, max_age_hours, force_recompute))
