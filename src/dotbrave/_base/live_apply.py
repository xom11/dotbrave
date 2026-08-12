"""Shared helpers for applying plans through a running browser."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from dotbrave._base.utils import Plan, backup_prefs, new_backup_path


MISSING = object()


class LiveApplyUnsupported(Exception):
    """A live adapter cannot apply one or more settings without restarting.

    ``backup_taken`` reports whether the adapter already took this run's
    single Preferences backup before it mutated anything.  The offline
    path that finishes the remainder must then skip its own backup: an
    adapter that applied part of the run live has to back up *before*
    that half lands, or the browser flushes it into the file the offline
    path would snapshot and ``--undo`` can only revert the offline
    remainder.  Defaults to False so a raiser that mutated nothing keeps
    the offline backup.
    """

    def __init__(
        self,
        browser_name: str,
        keys: list[str],
        *,
        backup_taken: bool = False,
    ) -> None:
        super().__init__(browser_name, keys)
        self.browser_name = browser_name
        self.keys = keys
        self.backup_taken = backup_taken


def compute_target_prefs(prefs: dict, plans: list[Plan]) -> dict:
    """Return the Preferences dict that normal offline apply would write."""
    target = copy.deepcopy(prefs)
    for plan in plans:
        if not plan.empty:
            plan.apply_fn(target)
    return target


def backup_preferences(prefs_path: Path) -> Path:
    backup = new_backup_path(prefs_path)
    backup_prefs(prefs_path, backup)
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
    adapters can split them out (see ``split_removals``) where the
    underlying API has no single-pref reset operation.
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


def split_removals(
    changes: list[tuple[tuple[str, ...], Any]],
) -> tuple[list[tuple[tuple[str, ...], Any]], list[str]]:
    """Separate applicable changes from deletions.

    Deleted leaves carry the ``MISSING`` sentinel.  A settings API with no
    single-pref reset cannot apply them live -- but they are the *only*
    part of the run that has to go offline, so return them rather than
    refusing the whole batch.
    """
    applicable = [(parts, value) for parts, value in changes if value is not MISSING]
    removals = [".".join(parts) for parts, value in changes if value is MISSING]
    return applicable, removals
