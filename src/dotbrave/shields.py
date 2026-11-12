"""Brave Shields per-site policy -- the ``[shields]`` table.

``disabled = [...]`` becomes Brave's ``BraveShieldsDisabledForUrls``
managed policy: Shields are off on every site that matches one of the
patterns (content-settings syntax, e.g. ``[*.]messenger.com``).

Like ``[pwa]`` this is external managed-policy storage written with
elevated privileges, never ``Preferences``. On macOS it shares the one
managed plist per bundle id with ``[pwa]`` (merged, and covered by the
same self-healing daemon); on Linux it is its own file beside
``dotbrave-pwa.json``, because each namespace there owns its whole file;
on Windows it is its own registry list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotbrave import pwa as pwa_mod
from dotbrave._base import pwa as _base
from dotbrave._base.utils import Plan

NAMESPACE = "shields"
POLICY_KEY = "BraveShieldsDisabledForUrls"

_LINUX_POLICY_PATH = "/etc/brave/policies/managed/dotbrave-shields.json"


def _default_policy_file() -> Path | None:
    if sys.platform.startswith("linux"):
        return Path(_LINUX_POLICY_PATH)
    if sys.platform == "darwin":
        return Path(pwa_mod._PWA_CONFIG.macos_plist_path)
    return None


# Module-level state that tests monkeypatch
POLICY_FILE = _default_policy_file()
_WINDOWS_POLICY_KEY = pwa_mod._WINDOWS_POLICY_KEY


def _read_current_policy() -> list[str]:
    if sys.platform == "win32":
        return _base.read_windows_registry_strings(_WINDOWS_POLICY_KEY, POLICY_KEY)
    data = _base.read_existing_payload(POLICY_FILE, _WINDOWS_POLICY_KEY)
    patterns = data.get(POLICY_KEY, [])
    if not isinstance(patterns, list):
        return []
    return [p for p in patterns if isinstance(p, str) and p]


def _sudo_write_policy(patterns: list[str]) -> None:
    _base.sudo_write_policy(
        POLICY_FILE, _WINDOWS_POLICY_KEY, patterns,
        policy_key=POLICY_KEY,
        managed_keys=pwa_mod.MANAGED_POLICY_KEYS,
    )


def validate_table(raw: object) -> list[str]:
    """The ``disabled`` patterns, in order, duplicates dropped.

    Pattern syntax is Brave's to judge -- an invalid one shows up as an
    error on brave://policy -- so only the shape is checked here."""
    if not isinstance(raw, dict):
        sys.exit("error: [shields] must be a table")
    extra = set(raw) - {"disabled"}
    if extra:
        sys.exit(
            f"error: [shields] has unsupported keys: {sorted(extra)}. "
            f"Only `disabled = [...]` is supported"
        )
    patterns = raw.get("disabled", [])
    if not isinstance(patterns, list):
        sys.exit("error: [shields] disabled must be an array of strings")
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        if not isinstance(p, str) or not p.strip() or p != p.strip():
            sys.exit(
                f"error: [shields] disabled entries must be non-empty strings "
                f"without surrounding whitespace, got {p!r}"
            )
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def diff_summary(current: list[str], target: list[str]) -> list[str]:
    lines = [f"  + {p} (Shields off)" for p in sorted(set(target) - set(current))]
    lines += [f"  - {p} (Shields back on)" for p in sorted(set(current) - set(target))]
    return lines


def _check_supported(prefs_path: Path) -> None:
    if sys.platform != "win32" and POLICY_FILE is None:
        sys.exit(
            f"error: [shields] is not implemented on platform={sys.platform!r}. "
            f"Linux, macOS and Windows are supported."
        )
    for substr, install_name, policy_dir in pwa_mod._PWA_CONFIG.sandbox_checks:
        if substr in str(prefs_path):
            sys.exit(
                f"error: [shields] is not supported on {install_name} (the "
                f"sandbox does not read {policy_dir}). Install Brave from the "
                f"official package, or remove the [shields] table from your "
                f"config."
            )


def plan_apply(prefs_path: Path, prefs: dict, raw_table: object) -> Plan:
    _check_supported(prefs_path)
    target = validate_table(raw_table)
    current = _read_current_policy()
    diff = diff_summary(current, target)
    dirty = bool(diff)

    def apply_fn(_prefs: dict) -> None:
        pass

    def verify_fn(_reloaded: dict) -> None:
        pass

    def external_apply_fn() -> None:
        if not dirty:
            return
        _sudo_write_policy(target)
        actual = _read_current_policy()
        if set(actual) != set(target):
            sys.exit(
                "error: shields verification failed: policy does not match "
                f"config (wrote {sorted(target)}, policy has {sorted(actual)})"
            )

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        external_apply_fn=external_apply_fn,
        needs_escalation=dirty,
    )


def build_dump_block() -> list[str] | None:
    """``[shields]`` for ``export``, or None while the policy is empty so a
    config that never used the table does not grow one."""
    current = _read_current_policy()
    if not current:
        return None
    lines = ["[shields]", "disabled = ["]
    lines += [f"  {json.dumps(p)}," for p in current]
    lines.append("]")
    return lines
