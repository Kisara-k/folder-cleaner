"""
formatter.py — Output formatting for folder-cleaner.

Renders tree lines and summary footer to both stdout and a report file.
"""

import datetime
import os
from typing import TextIO

from common import MB

# Tree drawing characters
_BRANCH = "├── "
_LAST   = "└── "
_VERT   = "│   "
_SPACE  = "    "

# Column widths
_NAME_WIDTH = 55
_SIZE_WIDTH = 14
_COUNT_WIDTH = 12


class Reporter:
    """Writes tree lines and summary to stdout + report file simultaneously."""

    def __init__(self, report_path: str):
        self._report_path = report_path
        self._file: Optional[TextIO] = None
        self._lines_written = 0

    def open(self) -> None:
        self._file = open(self._report_path, "w", encoding="utf-8")

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def _write(self, line: str) -> None:
        print(line)
        if self._file:
            self._file.write(line + "\n")
        self._lines_written += 1

    # ------------------------------------------------------------------
    # Tree rendering
    # ------------------------------------------------------------------

    def root_line(self, path: str, size_bytes: int, file_count: int) -> None:
        name = path
        self._write(_format_line(name, size_bytes, file_count, is_junk=False, prefix=""))

    def entry_line(
        self,
        name: str,
        size_bytes: int,
        file_count: int,
        depth: int,
        is_last: bool,
        is_junk: bool,
        prefix_flags: list[bool],  # True = has more siblings at that ancestor level
    ) -> None:
        """
        prefix_flags: one bool per ancestor level (True = draw vertical bar).
        len(prefix_flags) == depth - 1 (ancestors above this entry's parent).
        """
        prefix = _build_prefix(prefix_flags, is_last)
        self._write(_format_line(name, size_bytes, file_count, is_junk, prefix))

    def divider(self) -> None:
        self._write("─" * 72)

    def summary(
        self,
        root: str,
        total_bytes: int,
        total_files: int,
        junk_bytes: int,
        junk_count: int,
        duration: float,
        report_path: str,
    ) -> None:
        self.divider()
        self._write(f"Scan root:     {root}")
        self._write(f"Total size:    {_fmt_mb(total_bytes)}")
        self._write(f"Total files:   {total_files:,}")
        if junk_count > 0:
            self._write(f"Junk size:     {_fmt_mb(junk_bytes)} across {junk_count} junk dir(s)")
        self._write(f"Scan duration: {duration:.2f}s")
        self._write(f"Report saved:  {report_path}")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _build_prefix(prefix_flags: list[bool], is_last: bool) -> str:
    """Build the tree connector string."""
    parts = []
    for has_more in prefix_flags:
        parts.append(_VERT if has_more else _SPACE)
    parts.append(_LAST if is_last else _BRANCH)
    return "".join(parts)


def _format_line(
    name: str,
    size_bytes: int,
    file_count: int,
    is_junk: bool,
    prefix: str,
) -> str:
    full_name = prefix + name
    # Truncate name if too long
    display_name = full_name if len(full_name) <= _NAME_WIDTH else full_name[: _NAME_WIDTH - 3] + "..."
    size_str = _fmt_mb(size_bytes).rjust(_SIZE_WIDTH)
    count_str = f"{file_count:,} files".rjust(_COUNT_WIDTH)
    flags = ""
    if is_junk:
        flags += "  [JUNK - safe to delete]"
    return f"{display_name:<{_NAME_WIDTH}}  {size_str}  {count_str}{flags}"


def _fmt_mb(size_bytes: int) -> str:
    mb = size_bytes / MB
    if mb >= 1000:
        return f"{mb:,.1f} MB"
    return f"{mb:.2f} MB"


def make_report_path(target_dir: str) -> str:
    """Return timestamped report path inside a reports/ subdirectory next to the script.

    The target directory is encoded into the filename using '--' as the path
    separator replacement and dropping the ':' after Windows drive letters.
    This is fully reversible: split on '--', re-add ':' to the first segment.
    Example: C:\\GTarcade -> 20260605_143012_C--GTarcade.txt
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Drop drive colon (C: -> C), replace separators with --
    encoded = target_dir.replace(":\\", "_").replace("\\", "_").replace("/", "_")
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    return os.path.join(reports_dir, f"{ts}_{encoded}.txt")
