"""Brave PWA -- browser-specific config + thin wrappers.

Module-level ``POLICY_FILE`` and ``_sudo_write_policy`` are kept here
(not in ``_base``) so tests can monkeypatch them per-browser.  Read
functions form a chain through module-level names so patching any
function in the chain is visible to callers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotbrave._base import pwa as _base
from dotbrave._base.utils import Plan, find_preferences

NAMESPACE = _base.NAMESPACE
POLICY_KEY = _base.POLICY_KEY

_PWA_CONFIG = _base.PwaConfig(
    browser_name="brave",
    linux_policy_path="/etc/brave/policies/managed/dotbrave-pwa.json",
    macos_plist_path="/Library/Managed Preferences/com.brave.Browser.plist",
    windows_registry_key=r"Software\Policies\BraveSoftware\Brave",
    sandbox_checks=[
        (
            "/snap/brave/",
            "Snap Brave",
            "/etc/brave/policies/managed/",
        ),
        (
            "/.var/app/com.brave.Browser/",
            "Flatpak Brave",
            "/etc/brave/policies/managed/",
        ),
    ],
)

# Module-level state that tests monkeypatch
POLICY_FILE = _base.default_policy_file(_PWA_CONFIG)
_WINDOWS_POLICY_KEY = _PWA_CONFIG.windows_registry_key


# ---------------------------------------------------------------------------
# Wrappers that read module-level state.  Each function calls its siblings
# via the module namespace (not _base) so monkeypatching any one name in
# this module affects the whole chain.
# ---------------------------------------------------------------------------

def _read_existing_payload() -> dict:
    return _base.read_existing_payload(POLICY_FILE, _WINDOWS_POLICY_KEY)


def _read_current_policy() -> dict[str, dict]:
    """Chain through local _read_existing_payload so patches propagate."""
    data = _read_existing_payload()
    entries = data.get(POLICY_KEY, [])
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict] = {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("url"), str):
            out[e["url"]] = e
    return out


def _build_policy_payload(entries: list[dict]) -> bytes:
    return _base.build_policy_payload(POLICY_FILE, _WINDOWS_POLICY_KEY, entries)


def _entry_for(url: str) -> dict[str, Any]:
    return _base.entry_for(url)


def _sudo_write_policy(entries: list[dict]) -> None:
    _base.sudo_write_policy(POLICY_FILE, _WINDOWS_POLICY_KEY, entries)


def _validate_table(raw: object) -> list[str]:
    return _base.validate_table(raw)


def diff_summary(current: dict[str, dict], target_urls: list[str]) -> list[str]:
    return _base.diff_summary(current, target_urls)


def _check_platform_supported() -> None:
    _base.check_platform_supported(POLICY_FILE)


def _check_install_supported(prefs_path: Path) -> None:
    _base.check_install_supported(_PWA_CONFIG, prefs_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_apply(prefs_path: Path, prefs: dict, raw_table: object) -> Plan:
    return _base.plan_apply(
        _PWA_CONFIG,
        POLICY_FILE,
        _sudo_write_policy,
        _read_current_policy,
        prefs_path,
        prefs,
        raw_table,
    )


def build_dump_block(*, header_comment: str | None = None) -> list[str]:
    return _base.build_dump_block(
        POLICY_FILE, _WINDOWS_POLICY_KEY, _read_current_policy,
        header_comment=header_comment,
    )


def cmd_dump(args: argparse.Namespace) -> None:
    _base.cmd_dump(
        "brave", POLICY_FILE, _WINDOWS_POLICY_KEY,
        _read_current_policy, args,
    )


def register(subparsers: argparse._SubParsersAction, profile_args) -> None:
    _base.register(
        "brave", POLICY_FILE, _WINDOWS_POLICY_KEY,
        _read_current_policy, cmd_dump, subparsers, profile_args,
    )
