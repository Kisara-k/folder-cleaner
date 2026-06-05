"""
config.py — User configuration for folder-cleaner.
Edit this file, then run: python scanner.py
"""

from common import MB

TARGET_DIR                = r"C:\Users\ASUS\Documents"  # Directory to scan
MAX_DEPTH                 = 4               # Max levels to display (1 = top-level only)
INVALIDATE_CACHE          = False           # True = wipe cache and do full rescan
CACHE_MAX_AGE_HOURS       = 24             # Hours before cache entries auto-expire; 0 = no cache; -1 = never expire
RECOMPUTE_ROOT            = True            # Always recompute root size (ignores cache); ensures accuracy
MIN_DIR_SIZE_BYTES        = MB             # Don't show children of dirs smaller than this (1 MB)
NUM_WORKERS               = None           # None = min(cpu_count, 8); or set an int

# Absolute paths to never descend into (shown in report but not expanded).
# Use this for system directories where SKIP_DIRS name-matching is too broad.
SKIP_PATHS: frozenset[str] = frozenset({
    r"C:\Windows",
})

SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".next",
    ".pnpm-store",
    ".cache",
    "target",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "Pods",
    ".gradle",
})
