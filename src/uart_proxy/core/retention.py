"""
Session-store retention policy.

The default log root (``~/.uart-proxy/sessions/``) accumulates one folder per
run. This module prunes it along two axes:

* **age** — delete session folders older than ``max_age_days`` (default 30).
* **total size** — if the store still exceeds ``max_total_bytes`` (default
  500 MB), delete the **oldest** folders (logrotate-style) until it fits.

Either axis can be disabled by passing ``0``. The currently-active session can
be shielded via ``protect`` so a long run is never deleted underneath itself.

The functions are pure with respect to the filesystem and injectable ``now``,
which keeps them unit-testable.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_TOTAL_MB = 500
DEFAULT_MAX_TOTAL_BYTES = DEFAULT_MAX_TOTAL_MB * 1024 * 1024


@dataclass
class SessionInfo:
    path: str
    mtime: float
    size: int


@dataclass
class RetentionReport:
    deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    kept: int = 0
    total_before: int = 0
    total_after: int = 0


def dir_size(path: str) -> int:
    """Total size in bytes of all files under ``path``."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def scan_sessions(root: str) -> list[SessionInfo]:
    """List immediate sub-folders of ``root`` with their mtime and size."""
    out: list[SessionInfo] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append(SessionInfo(path=path, mtime=mtime, size=dir_size(path)))
    return out


def apply_retention(
    root: str,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    now: float | None = None,
    protect: list[str] | None = None,
) -> RetentionReport:
    """Prune ``root`` by age then by total size. Returns what was removed."""
    now = time.time() if now is None else now
    protected = {os.path.abspath(p) for p in (protect or [])}
    sessions = scan_sessions(root)

    report = RetentionReport(total_before=sum(s.size for s in sessions))

    # 1) Age-based pruning.
    survivors: list[SessionInfo] = []
    for s in sessions:
        age_days = (now - s.mtime) / 86400.0
        if (
            max_age_days
            and age_days > max_age_days
            and os.path.abspath(s.path) not in protected
        ):
            _remove(s.path, s.size, report)
        else:
            survivors.append(s)

    # 2) Size-based pruning: drop oldest until under the cap.
    if max_total_bytes:
        total = sum(s.size for s in survivors)
        for s in sorted(survivors, key=lambda x: x.mtime):  # oldest first
            if total <= max_total_bytes:
                break
            if os.path.abspath(s.path) in protected:
                continue
            _remove(s.path, s.size, report)
            total -= s.size

    remaining = [s for s in survivors if s.path not in report.deleted]
    report.kept = len(remaining)
    report.total_after = sum(s.size for s in remaining)
    return report


def _remove(path: str, size: int, report: RetentionReport) -> None:
    shutil.rmtree(path, ignore_errors=True)
    report.deleted.append(path)
    report.freed_bytes += size


def format_bytes(n: int) -> str:
    """Human-readable byte count (e.g. '1.5 MB')."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
