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
# Every key dotbrave writes into the one macOS managed plist ([shields]
# shares it); see ``_base.sudo_write_policy``.
MANAGED_POLICY_KEYS = (POLICY_KEY, "BraveShieldsDisabledForUrls")

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


def _entry_for(url: str, name: str | None = None) -> dict[str, Any]:
    return _base.entry_for(url, name)


def _sudo_write_policy(entries: list[dict]) -> None:
    _base.sudo_write_policy(
        POLICY_FILE, _WINDOWS_POLICY_KEY, entries,
        managed_keys=MANAGED_POLICY_KEYS,
    )


def _validate_table(raw: object) -> list[str]:
    return _base.validate_table(raw)


def diff_summary(current: dict[str, dict], target_urls: list[str]) -> list[str]:
    return _base.diff_summary(current, target_urls)


def _check_platform_supported() -> None:
    _base.check_platform_supported(POLICY_FILE)


def _check_install_supported(prefs_path: Path) -> None:
    _base.check_install_supported(_PWA_CONFIG, prefs_path)


_BUNDLE_CHANNEL = {
    "com.brave.Browser": "stable",
    "com.brave.Browser.beta": "beta",
    "com.brave.Browser.nightly": "nightly",
}


def _launcher_heal() -> _base.LauncherHeal | None:
    if not sys.platform.startswith("linux"):
        return None
    return _base.LauncherHeal(
        current=lambda: _base.linux_heal_current(_base.linux_heal_paths()),
        present=lambda: _base.linux_heal_present(_base.linux_heal_paths()),
        install=lambda: _base.install_linux_launcher_heal(_base.linux_heal_paths()),
        remove=lambda: _base.remove_linux_launcher_heal(_base.linux_heal_paths()),
    )


def _launcher_cleanup(
    prefs_path: Path, prefs: dict
) -> _base.LauncherCleanup | None:
    if sys.platform.startswith("linux"):
        from dotbrave.browser import CHANNELS, _default_profile_root

        profile_root = prefs_path.parent.parent
        is_default = any(
            (d := _default_profile_root(ch)) is not None
            and _base._path_within(profile_root, d)
            and _base._path_within(d, profile_root)
            for ch in CHANNELS
        )
        if not is_default:
            return None
        return _base.LauncherCleanup(
            orphan_diff=lambda: [],
            clean=lambda removed, target: _base.cleanup_linux_removed_launchers(
                _base.linux_heal_paths(),
                prefs_path.parent.name,
                removed,
                target,
                prefs,
            ),
        )
    if sys.platform == "darwin" and POLICY_FILE is not None:
        from dotbrave.browser import _default_profile_root

        bundle_id = _base.macos_bundle_id(POLICY_FILE)
        shim_dir = _base.macos_shim_dir(bundle_id)
        channel = _BUNDLE_CHANNEL.get(bundle_id)
        if shim_dir is None or channel is None:
            return None
        default_root = _default_profile_root(channel)
        profile_root = prefs_path.parent.parent
        if (
            default_root is None
            or not _base._path_within(profile_root, default_root)
            or not _base._path_within(default_root, profile_root)
        ):
            return None
        return _base.LauncherCleanup(
            orphan_diff=lambda: [
                f"  - {p.name} (foreign profile shim)"
                for p in _base.scan_macos_foreign_shims(
                    shim_dir, bundle_id, profile_root
                )
            ],
            clean=lambda removed, target: _base.cleanup_macos_shims(
                shim_dir,
                bundle_id,
                profile_root,
                removed,
                target,
                prefs,
                clean_foreign=True,
            ),
        )
    return None


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
        _launcher_heal(),
        _launcher_cleanup(prefs_path, prefs),
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
