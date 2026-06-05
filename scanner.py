"""
scanner.py — Folder Size Analyzer & Cleanup Assistant
======================================================
Edit config.py to set your target directory and options, then run:
    python scanner.py
"""

import heapq
import multiprocessing
import os
import subprocess
import sys
import time
from typing import Optional

import config
import formatter as fmt_mod
import worker as worker_mod
from common import get_inode, norm_path

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
        "children": children,  # None = not expanded; [] = expanded but empty
    }


# ------------------------------------------------------------------------------
# Size computation via system commands (fast and reliable)
# ------------------------------------------------------------------------------

def _get_dir_size_system(path: str) -> tuple[int, int]:
    """
    Get directory size and file count using system commands.
    Returns (size_bytes, file_count). Falls back to (0, 0) if unavailable.
    """
    try:
        if sys.platform == "win32":
            # Windows: PowerShell
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
                file_count = _count_files_system(path)
                return size_bytes, file_count
        else:
            # Unix/Linux: du and find
            result = subprocess.run(
                ["du", "-sb", path],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                size_bytes = int(result.stdout.split()[0])
                file_count = _count_files_system(path)
                return size_bytes, file_count
    except Exception:
        pass

    return 0, 0


def _count_files_system(path: str) -> int:
    """Count files recursively using system commands."""
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
                count = result.stdout.strip()
                return int(count) if count else 0
        else:
            result = subprocess.run(
                ["find", path, "-type", "f"],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                return len([l for l in result.stdout.strip().split('\n') if l])
    except Exception:
        pass

    return 0


# ------------------------------------------------------------------------------
# Tree builder (produces nested node structure up to MAX_DEPTH)
# ------------------------------------------------------------------------------

def build_tree(
    path: str,
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
        return _make_node(path, name, 0, 0, 0.0, None, True, False, [])

    mtime = st.st_mtime
    inode = get_inode(st)
    is_junk = name in config.SKIP_DIRS or path in config.SKIP_PATHS

    # Use system command for accurate size (especially for root)
    size_bytes, file_count = _get_dir_size_system(path)

    children: Optional[list[dict]] = None
    if depth < config.MAX_DEPTH and not is_junk and size_bytes >= config.MIN_DIR_SIZE_BYTES:
        children = _expand_children(path, pool, depth)

    return _make_node(
        path=path,
        name=name,
        size_bytes=size_bytes,
        file_count=file_count,
        mtime=mtime,
        inode=inode,
        is_dir=True,
        is_junk=is_junk,
        children=children if children is not None else [],
    )


def _expand_children(
    path: str,
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
            file_nodes.append(_make_node(
                path=norm_path(entry.path),
                name=entry.name,
                size_bytes=st.st_size,
                file_count=1,
                mtime=st.st_mtime,
                inode=get_inode(st),
                is_dir=False,
                is_junk=False,
                children=None,
            ))
        elif entry.is_dir(follow_symlinks=False):
            dir_paths.append((norm_path(entry.path), entry.name))

    children: list[dict] = []
    for dir_path, _ in dir_paths:
        children.append(build_tree(dir_path, pool, depth + 1))
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

    report_path = fmt_mod.make_report_path(target)

    print(f"Folder Analyzer — scanning: {target}")
    print(f"Max depth: {config.MAX_DEPTH}")
    print()

    start = time.perf_counter()

    n_workers = config.NUM_WORKERS if config.NUM_WORKERS is not None else min(os.cpu_count() or 1, 8)
    pool: Optional[multiprocessing.Pool] = None
    if n_workers > 1:
        pool = multiprocessing.Pool(processes=n_workers)

    try:
        root_node = build_tree(target, pool, depth=0)

        reporter = fmt_mod.Reporter(report_path)
        reporter.open()

        stats = {"total_bytes": 0, "total_files": 0, "junk_bytes": 0, "junk_count": 0}
        render_tree(root_node, reporter, prefix_flags=[], depth=0, is_last=True, stats=stats)

        elapsed = time.perf_counter() - start

        reporter.summary(
            root=target,
            total_bytes=stats["total_bytes"],
            total_files=stats["total_files"],
            junk_bytes=stats["junk_bytes"],
            junk_count=stats["junk_count"],
            duration=elapsed,
            report_path=report_path,
        )
        reporter.close()

    finally:
        if pool is not None:
            pool.close()
            pool.join()


if __name__ == "__main__":
    main()
