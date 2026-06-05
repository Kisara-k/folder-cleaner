"""
scanner.py — Folder Size Analyzer & Cleanup Assistant
======================================================
Edit config.py to set your target directory and options, then run:
    python scanner.py
"""

import heapq
import multiprocessing
import os
import sys
import time
from typing import Optional

import cache as cache_mod
import config
import formatter as fmt_mod
import worker as worker_mod
from common import db_file_path, get_inode, norm_path

_SUBTREE_WORKER_THRESHOLD = 1  # dispatch to worker pool if depth remaining > this


# ------------------------------------------------------------------------------
# Node data class (lightweight dict-based to avoid dataclass overhead)
# ------------------------------------------------------------------------------

def _make_node(
    path: str,
    name: str,
    size_bytes: int,
    file_count: int,
    mtime: float,
    inode: Optional[int],
    is_dir: bool,
    is_junk: bool,
    cached_at: Optional[float],
    children: Optional[list] = None,
) -> dict:
    return {
        "path": path,
        "name": name,
        "size_bytes": size_bytes,
        "file_count": file_count,
        "mtime": mtime,
        "inode": inode,
        "is_dir": is_dir,
        "is_junk": is_junk,
        "cached_at": cached_at,
        "children": children,  # None = not expanded; [] = expanded but empty
    }


# ------------------------------------------------------------------------------
# Subtree size computation (main-process, cache-aware)
# ------------------------------------------------------------------------------

def _compute_dir_size(
    path: str,
    db: cache_mod.CacheDB,
    db_path: str,
    pool: Optional[multiprocessing.Pool],
    depth_remaining: int,
    force_recompute: bool = False,
) -> tuple[int, int, list[dict]]:
    """
    Compute (size_bytes, file_count, children_nodes) for a directory.
    Uses cache when valid (unless force_recompute=True). Falls back to iterative scandir DFS.
    """
    path = norm_path(path)

    try:
        st = os.stat(path)
    except OSError:
        return 0, 0, []

    mtime = st.st_mtime

    if not force_recompute:
        cached = db.lookup(path, mtime)
        if cached is not None:
            size_bytes, file_count, _ = cached
            return size_bytes, file_count, []

    if pool is not None and depth_remaining > _SUBTREE_WORKER_THRESHOLD:
        results = _dispatch_to_worker(path, mtime, db_path, pool, force_recompute)
    else:
        results = _scan_dir_iterative(path, db_path, force_recompute)

    for r in results:
        db.store(
            path=r["path"],
            size_bytes=r["size_bytes"],
            file_count=r["file_count"],
            mtime=r["mtime"],
            inode=r["inode"],
            is_dir=r["is_dir"],
            is_junk=r["is_junk"],
        )
    db.flush()

    for r in results:
        if norm_path(r["path"]) == path:
            return r["size_bytes"], r["file_count"], []

    return 0, 0, []


def _dispatch_to_worker(
    path: str,
    mtime: float,
    db_path: str,
    pool: multiprocessing.Pool,
    force_recompute: bool = False,
) -> list[dict]:
    """Send subtree computation to a worker process."""
    args = (path, mtime, db_path, config.CACHE_MAX_AGE_HOURS, force_recompute)
    try:
        return pool.apply(worker_mod.compute_subtree, (args,))
    except Exception as e:
        print(f"[WARN] Worker failed for {path}: {e}. Falling back to in-process scan.")
        worker_mod.SKIP_DIRS = config.SKIP_DIRS
        worker_mod.SKIP_PATHS = config.SKIP_PATHS
        return worker_mod.compute_subtree(args)


def _scan_dir_iterative(root: str, db_path: str, force_recompute: bool = False) -> list[dict]:
    """Iterative post-order DFS via worker engine (in-process)."""
    worker_mod.SKIP_DIRS = config.SKIP_DIRS
    worker_mod.SKIP_PATHS = config.SKIP_PATHS
    try:
        root_st = os.stat(root)
    except OSError:
        return []
    return worker_mod.compute_subtree(
        (root, root_st.st_mtime, db_path, config.CACHE_MAX_AGE_HOURS, force_recompute)
    )


# ------------------------------------------------------------------------------
# Tree builder (produces nested node structure up to MAX_DEPTH)
# ------------------------------------------------------------------------------

def build_tree(
    path: str,
    db: cache_mod.CacheDB,
    db_path: str,
    pool: Optional[multiprocessing.Pool],
    depth: int = 0,
) -> dict:
    """
    Build a tree node for `path` at the given depth.
    Children are expanded only if depth < MAX_DEPTH and dir is large enough.
    """
    path = norm_path(path)
    name = os.path.basename(path) or path

    try:
        st = os.stat(path)
    except OSError:
        return _make_node(path, name, 0, 0, 0.0, None, True, False, None, [])

    mtime = st.st_mtime
    inode = get_inode(st)
    is_junk = name in config.SKIP_DIRS or path in config.SKIP_PATHS

    # Force recompute root if configured (ensures total size accuracy)
    force_recompute = depth == 0 and config.RECOMPUTE_ROOT
    is_root = depth == 0
    size_bytes, file_count = _get_size_and_count(path, mtime, db, db_path, pool, force_recompute, is_root)
    cached_at = db.get_cached_at(path)

    children: Optional[list[dict]] = None
    if depth < config.MAX_DEPTH and not is_junk and size_bytes >= config.MIN_DIR_SIZE_BYTES:
        children = _expand_children(path, db, db_path, pool, depth)

    return _make_node(
        path=path,
        name=name,
        size_bytes=size_bytes,
        file_count=file_count,
        mtime=mtime,
        inode=inode,
        is_dir=True,
        is_junk=is_junk,
        cached_at=cached_at,
        children=children if children is not None else [],
    )


def _get_size_and_count(
    path: str,
    mtime: float,
    db: cache_mod.CacheDB,
    db_path: str,
    pool: Optional[multiprocessing.Pool],
    force_recompute: bool = False,
    is_root: bool = False,
) -> tuple[int, int]:
    if not force_recompute:
        cached = db.lookup(path, mtime)
        if cached is not None:
            return cached[0], cached[1]

    # For root directory with force_recompute, use system command for accuracy
    if force_recompute and is_root:
        import subprocess
        import sys

        try:
            if sys.platform == "win32":
                ps_cmd = (
                    f"$sum = (Get-ChildItem -Path '{path}' -Recurse -Force -ErrorAction SilentlyContinue | "
                    f"Measure-Object -Property Length -Sum).Sum; "
                    f"if ($null -eq $sum) {{ 0 }} else {{ $sum }}"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if result.returncode == 0:
                    size_bytes = int(result.stdout.strip() or 0)
                    # Count files recursively
                    file_count = _count_files_fast(path)
                    db.store(path, size_bytes, file_count, mtime, None, True, False)
                    db.flush()
                    return size_bytes, file_count
            else:
                # Unix/Linux
                result = subprocess.run(
                    ["du", "-sb", path],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if result.returncode == 0:
                    size_bytes = int(result.stdout.split()[0])
                    file_count = _count_files_fast(path)
                    db.store(path, size_bytes, file_count, mtime, None, True, False)
                    db.flush()
                    return size_bytes, file_count
        except Exception:
            pass  # Fallback to worker

    size_bytes, file_count, _ = _compute_dir_size(path, db, db_path, pool, config.MAX_DEPTH, force_recompute)
    return size_bytes, file_count


def _count_files_fast(path: str) -> int:
    """Count files recursively using system command."""
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            ps_cmd = f"(Get-ChildItem -Path '{path}' -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object).Count"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                return int(result.stdout.strip() or 0)
        else:
            result = subprocess.run(
                ["find", path, "-type", "f"],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                return len(result.stdout.strip().split('\n'))
    except Exception:
        pass

    return 0


def _expand_children(
    path: str,
    db: cache_mod.CacheDB,
    db_path: str,
    pool: Optional[multiprocessing.Pool],
    depth: int,
) -> list[dict]:
    """Scan one level of children and build their nodes."""
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError as e:
        print(f"[WARN] Cannot scan {path}: {e}")
        return []

    file_nodes: list[dict] = []
    dir_paths: list[tuple[str, str]] = []

    for entry in entries:
        if entry.is_symlink():
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue

        if entry.is_file(follow_symlinks=False):
            if st.st_size < config.MIN_FILE_SIZE_BYTES:
                continue
            file_nodes.append(_make_node(
                path=norm_path(entry.path),
                name=entry.name,
                size_bytes=st.st_size,
                file_count=1,
                mtime=st.st_mtime,
                inode=get_inode(st),
                is_dir=False,
                is_junk=False,
                cached_at=None,
                children=None,
            ))
        elif entry.is_dir(follow_symlinks=False):
            dir_paths.append((norm_path(entry.path), entry.name))

    children: list[dict] = []
    for dir_path, _ in dir_paths:
        children.append(build_tree(dir_path, db, db_path, pool, depth + 1))
    children.extend(file_nodes)

    return _sort_children(children)


def _sort_children(children: list[dict]) -> list[dict]:
    key = lambda n: n["size_bytes"]
    if len(children) > 1000:
        return heapq.nlargest(len(children), children, key=key)
    return sorted(children, key=key, reverse=True)


# ------------------------------------------------------------------------------
# Renderer — streams output as tree is walked
# ------------------------------------------------------------------------------

def render_tree(
    node: dict,
    reporter: fmt_mod.Reporter,
    prefix_flags: list[bool],
    depth: int,
    is_last: bool,
    stats: dict,
) -> None:
    """Recursively render node + children, streaming each line."""
    if depth == 0:
        reporter.root_line(
            path=node["path"],
            size_bytes=node["size_bytes"],
            file_count=node["file_count"],
            cached_at=node["cached_at"],
        )
        stats["total_bytes"] = node["size_bytes"]
        stats["total_files"] = node["file_count"]
    else:
        reporter.entry_line(
            name=node["name"],
            size_bytes=node["size_bytes"],
            file_count=node["file_count"],
            depth=depth,
            is_last=is_last,
            is_junk=node["is_junk"],
            cached_at=node["cached_at"],
            prefix_flags=prefix_flags,
        )

    if node["is_junk"]:
        stats["junk_bytes"] += node["size_bytes"]
        stats["junk_count"] += 1

    children = node.get("children") or []
    for i, child in enumerate(children):
        child_is_last = (i == len(children) - 1)
        child_prefix_flags = [] if depth == 0 else prefix_flags + [not is_last]
        render_tree(child, reporter, child_prefix_flags, depth + 1, child_is_last, stats)


# ------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------

def main() -> None:
    target = norm_path(config.TARGET_DIR)
    if not os.path.isdir(target):
        print(f"[ERROR] Target directory not found: {target}")
        sys.exit(1)

    db_path = db_file_path()
    report_path = fmt_mod.make_report_path(target)

    print(f"Folder Analyzer — scanning: {target}")
    print(f"Max depth: {config.MAX_DEPTH}  |  Cache: {'INVALIDATED' if config.INVALIDATE_CACHE else 'enabled'}")
    print()

    start = time.perf_counter()

    db = cache_mod.open_cache(
        invalidate=config.INVALIDATE_CACHE,
        max_age_hours=config.CACHE_MAX_AGE_HOURS,
    )

    n_workers = config.NUM_WORKERS if config.NUM_WORKERS is not None else min(os.cpu_count() or 1, 8)
    pool: Optional[multiprocessing.Pool] = None
    if n_workers > 1:
        pool = multiprocessing.Pool(
            processes=n_workers,
            initializer=worker_mod._pool_init,
            initargs=(config.SKIP_DIRS, config.SKIP_PATHS),
        )

    try:
        root_node = build_tree(target, db, db_path, pool, depth=0)

        reporter = fmt_mod.Reporter(report_path)
        reporter.open()

        stats = {"total_bytes": 0, "total_files": 0, "junk_bytes": 0, "junk_count": 0}
        render_tree(root_node, reporter, prefix_flags=[], depth=0, is_last=True, stats=stats)

        elapsed = time.perf_counter() - start

        db.update_meta("last_full_scan_time", str(time.time()))
        db.update_meta("scan_root", target)

        reporter.summary(
            root=target,
            total_bytes=stats["total_bytes"],
            total_files=stats["total_files"],
            junk_bytes=stats["junk_bytes"],
            junk_count=stats["junk_count"],
            duration=elapsed,
            cache_hits=db.hits,
            cache_misses=db.misses,
            report_path=report_path,
        )
        reporter.close()

    finally:
        if pool is not None:
            pool.close()
            pool.join()
        db.close()


if __name__ == "__main__":
    main()
