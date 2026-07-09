"""Shared helpers for applying plans through a running browser."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from dotbrave._base.utils import Plan


MISSING = object()


class LiveApplyUnsupported(Exception):
    """A live adapter cannot apply one or more settings without restarting."""

    def __init__(self, browser_name: str, keys: list[str]) -> None:
        super().__init__(browser_name, keys)
        self.browser_name = browser_name
        self.keys = keys


def compute_target_prefs(prefs: dict, plans: list[Plan]) -> dict:
    """Return the Preferences dict that normal offline apply would write."""
    target = copy.deepcopy(prefs)
    for plan in plans:
        if not plan.empty:
            plan.apply_fn(target)
    return target


def backup_preferences(prefs_path: Path) -> Path:
    backup = prefs_path.with_suffix(
        prefs_path.suffix + f".bak.{datetime.now():%Y%m%d-%H%M%S}"
    )
    shutil.copy2(prefs_path, backup)
    print(f"backup: {backup}")
    return backup


def apply_external_plans(plans: list[Plan]) -> None:
    for plan in plans:
        if plan.external_apply_fn is not None and not plan.empty:
            plan.external_apply_fn()


def write_state_files(plans: list[Plan]) -> None:
    for plan in plans:
        if plan.state_path is not None:
            plan.state_path.write_text(
                json.dumps(plan.state_payload, indent=2), encoding="utf-8",
            )


def get_path(data: dict, parts: tuple[str, ...]) -> Any:
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def changed_leaf_paths(
    before: Any, after: Any, prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    """Return changed leaves in ``after`` compared to ``before``.

    Deleted leaves are reported with ``MISSING`` as the value so browser
    adapters can refuse live resets where the underlying API has no
    single-pref reset operation.
    """
    if isinstance(after, dict) and not isinstance(before, dict):
        before = {}
    if isinstance(before, dict) and after is MISSING:
        after = {}
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[tuple[tuple[str, ...], Any]] = []
        for key in sorted(set(before) | set(after)):
            b = before.get(key, MISSING)
            a = after.get(key, MISSING)
            out.extend(changed_leaf_paths(b, a, prefix + (str(key),)))
        return out
    if before != after:
        return [(prefix, after)]
    return []


def refuse_live_removals(
    browser_name: str,
    changes: list[tuple[tuple[str, ...], Any]],
) -> None:
    removals = [".".join(parts) for parts, value in changes if value is MISSING]
    if removals:
        raise LiveApplyUnsupported(browser_name, removals)
