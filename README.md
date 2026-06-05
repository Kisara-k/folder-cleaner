# folder-cleaner

Scans a directory tree and prints a size-sorted report showing what's eating your disk. Flags junk directories (node_modules, venv, etc.) and caches results for fast re-runs.

## Usage

Edit `config.py` to set your target directory and options, then run:

```
python scanner.py
```

Report is printed to stdout and saved as `reports/YYYYMMDD_HHMMSS.txt`.

## Config

Edit `config.py`:

```python
TARGET_DIR          = r"D:\Projects"  # Directory to scan
MAX_DEPTH           = 4               # Tree depth to display
INVALIDATE_CACHE    = False           # True = wipe cache, full rescan
CACHE_MAX_AGE_HOURS = 24              # 0 = disable cache; -1 = never auto-expire
NUM_WORKERS         = None            # None = min(cpu_count, 8)
```

## Output

```
D:\Projects                                       12,400.30 MB    183,241 files
├── node_modules                                     512.40 MB     18,234 files  [JUNK - safe to delete]
├── src                                              120.20 MB      3,400 files  [cached 2026-06-05 14:30]
│   ├── components                                    80.10 MB      2,100 files
│   └── utils                                         40.10 MB      1,300 files
└── dist                                              18.00 MB        340 files  [JUNK - safe to delete]
────────────────────────────────────────────────────────────────────────
Scan root:     D:\Projects
Total size:    12,400.30 MB
Total files:   183,241
Junk size:     530.40 MB across 2 junk dir(s)
Scan duration: 4.21s
Cache status:  847 entries reused, 23 rescanned
```

## Junk directories (never recursed into)

`node_modules` · `.venv` / `venv` · `__pycache__` · `.git` · `.next` · `dist` · `build` · `target` · `.gradle` · `Pods` · `.tox` · `.cache` · `.pnpm-store` · `.mypy_cache` · `.pytest_cache` · `.ruff_cache`

Add or remove entries via the `SKIP_DIRS` set in `scanner.py`.

## Files

| File | Purpose |
|---|---|
| `config.py` | User configuration — edit this |
| `scanner.py` | Entry point |
| `common.py` | Shared constants and utilities |
| `cache.py` | SQLite cache layer |
| `worker.py` | Multiprocessing subtree computation |
| `formatter.py` | Tree rendering and report output |
| `reports/` | Report output directory (auto-created) |
| `.dirsize_cache.db` | Cache file (auto-created) |

## Requirements

Python 3.10+. No external dependencies.
