"""Shared data structures and utilities for all browser modules."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
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


def load_prefs(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def get_nested(d: dict, keys: tuple[str, ...]) -> dict:
    for k in keys:
        d = d.setdefault(k, {})
    return d


def write_atomic(path: Path, prefs: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(prefs, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
