"""Shared data structures and utilities for all browser modules."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class Plan:
    """An applied-or-dry-run-able set of changes from one module.

    Each module's ``plan_apply`` returns one of these.  The unified
    ``<browser> apply`` orchestrator collects plans from every module that
    has a corresponding TOML table, prints their diffs, and (if not
    dry-run) runs all ``apply_fn``s against a single in-memory
    ``Preferences`` dict before a single ``write_atomic``.  State
    sidecars are written afterwards, and ``verify_fn``s run against the
    reloaded prefs.

    Most modules persist to the profile ``Preferences`` JSON via
    ``apply_fn`` and a sidecar at ``state_path``.  Modules that own
    external persistence (e.g. ``pwa``, which writes the managed-policy
    file) leave ``state_path``/``state_payload`` as None and do their
    write inside ``external_apply_fn``.
    """

    namespace: str
    diff_lines: list[str]
    apply_fn: Callable[[dict], None]
    verify_fn: Callable[[dict], None]
    state_path: Path | None = None
    state_payload: dict[str, Any] | None = None
    external_apply_fn: Callable[[], None] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.diff_lines


def find_preferences(profile_root: Path, profile: str) -> Path:
    p = profile_root / profile / "Preferences"
    if not p.exists():
        sys.exit(f"error: Preferences not found at {p}")
    return p


_PREFS_IO_ATTEMPTS = 5
_PREFS_IO_BACKOFF = 0.1


def retry_on_permission_error(op: Callable[[], Any], path: Path) -> Any:
    """Run ``op``, retrying while the browser is rewriting ``path``.

    Chromium replaces Preferences instead of writing it in place.  On
    Windows, touching the path while that replace is still pending fails
    with ERROR_ACCESS_DENIED -- surfacing as ``PermissionError`` -- for a
    few milliseconds at a time.  A ``[pwa]`` apply provokes exactly this:
    the freshly written policy makes the browser install the forced web
    app, which rewrites Preferences.

    Every read, backup copy and replace of Preferences goes through here,
    so a transient collision costs a retry instead of the whole apply.
    """
    for attempt in range(_PREFS_IO_ATTEMPTS):
        try:
            return op()
        except PermissionError:
            if attempt == _PREFS_IO_ATTEMPTS - 1:
                sys.exit(
                    f"error: permission denied accessing {path} after "
                    f"{_PREFS_IO_ATTEMPTS} attempts.\n"
                    "The browser rewrites this file as it runs; retry in a "
                    "moment, or quit the browser first."
                )
            time.sleep(_PREFS_IO_BACKOFF * 2**attempt)
    raise AssertionError("unreachable")


def load_prefs(path: Path) -> dict:
    def read() -> dict:
        with path.open(encoding="utf-8") as f:
            return json.load(f)

    return retry_on_permission_error(read, path)


def new_backup_path(prefs_path: Path) -> Path:
    """Return a backup path next to ``prefs_path`` that does not exist yet.

    Two backups must never share a name: ``shutil.copy2`` truncates its
    destination, so a reused name drops the earlier snapshot with no
    error.  Microseconds make a shared name effectively impossible; the
    counter makes it impossible, including across a DST fallback where a
    whole wall-clock second repeats.  Every field is fixed width, so a
    byte sort of these names is a chronological sort -- which is what
    ``cmd_restore`` relies on to pick the most recent one.

    Second-resolution names written by older versions still match the
    ``.bak.*`` glob and still sort correctly: ``...-000000`` is a prefix
    of ``...-000000.000001``, so it orders first, and an old-format file
    in the same second necessarily predates a new-format one.
    """
    base = f"{prefs_path.name}.bak.{datetime.now():%Y%m%d-%H%M%S.%f}"
    candidate = prefs_path.with_name(base)
    n = 0
    while candidate.exists():
        # Deliberately not re-reading the clock: under a frozen test
        # clock -- or a clock that simply has not ticked -- that loop
        # never terminates.
        n += 1
        candidate = prefs_path.with_name(f"{base}.{n:03d}")
    return candidate


def backup_prefs(prefs_path: Path, backup: Path) -> None:
    """Snapshot Preferences to ``backup`` while the browser may be running."""
    retry_on_permission_error(lambda: shutil.copy2(prefs_path, backup), prefs_path)
    # copy2 runs copystat, which copies Preferences' mtime onto the
    # backup -- so `restore --list` printed when the *browser* last wrote
    # the profile, contradicting the timestamp in the name beside it.  A
    # backup's mtime is when it was taken.  copy2 stays (copystat also
    # carries the mode, keeping a 0600 profile file 0600 rather than
    # picking up umask defaults); the utime sits outside the retry
    # because the file it touches is ours, not the one the browser is
    # racing.
    os.utime(backup, None)


def restore_prefs(backup: Path, prefs_path: Path) -> None:
    """Copy ``backup`` back over Preferences."""

    def _copy_and_touch() -> None:
        shutil.copy2(backup, prefs_path)
        # Unlike `backup_prefs`, `prefs_path` here IS Preferences -- the
        # file the browser's own replace can be racing -- so the utime
        # has to sit inside the retry too, not outside it: an unretried
        # `os.utime` left the same window `copy2` above it needs
        # `retry_on_permission_error` for at all, and a `PermissionError`
        # out of it would propagate uncaught after Preferences had
        # already been overwritten but before the sidecars were cleared
        # or the browser relaunched. `copy2` is idempotent, so a retry
        # here just re-copies and re-stamps; without this, the restored
        # Preferences would also carry the backup's mtime, which the next
        # backup would then inherit in turn.
        os.utime(prefs_path, None)

    retry_on_permission_error(_copy_and_touch, prefs_path)


def get_nested(d: dict, keys: tuple[str, ...]) -> dict:
    for k in keys:
        d = d.setdefault(k, {})
    return d


def write_atomic(path: Path, prefs: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(prefs, f, separators=(",", ":"), ensure_ascii=False)
    retry_on_permission_error(lambda: os.replace(tmp, path), path)
