"""Brave settings -- thin wrapper around shared settings logic.

All the real logic lives in ``dotbrave._base.settings``.  This module
just passes ``"brave"`` as the browser name for user-facing strings.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from dotbrave._base import settings as _base
from dotbrave._base.utils import Plan

NAMESPACE = _base.NAMESPACE

# Re-export internals that tests use directly
_MISSING = _base._MISSING
_split_key = _base._split_key
_get_value = _base._get_value
_set_value = _base._set_value
_pop_value = _base._pop_value
_is_mac_protected = _base._is_mac_protected
_validate_table = _base._validate_table
_get_managed_keys = _base._get_managed_keys
_format_toml_value = _base._format_toml_value
diff_summary = _base.diff_summary


def plan_apply(prefs_path: Path, prefs: dict, raw_table: object) -> Plan:
    return _base.plan_apply("brave", prefs_path, prefs, raw_table)


def cmd_dump(args: argparse.Namespace) -> None:
    _base.cmd_dump("brave", args)


def cmd_blocked(args: argparse.Namespace) -> None:
    _base.cmd_blocked("brave", args)


def cmd_snapshot(args: argparse.Namespace) -> None:
    _base.cmd_snapshot("brave", args)


# User-facing pref keys safe to export without a snapshot baseline.
# Entries are dotted-key prefixes matched by startswith(); exact keys are
# prefixes that only match themselves.  Chromium/Brave never rename pref
# strings once shipped (they persist in user profiles), so this list only
# ever needs additions for new features -- a missing entry means a key is
# absent from export, never wrong output.
KNOWN_SETTINGS: tuple[str, ...] = (
    "bookmark_bar.",
    "brave.tabs.",
    "brave.new_tab_page.show_",
    "brave.new_tab_page.hide_all_widgets",
    "brave.location_bar_is_wide",
    "brave.show_side_panel_button",
    "brave.today.should_show_toolbar_button",
    "brave.today.opted_in",
    "brave.wayback_machine_enabled",
    "omnibox.prevent_url_elisions",
    "browser.show_home_button",
)


def build_export_lines(
    args: argparse.Namespace, prefs_path: Path, prefs: dict
) -> list[str]:
    return _base.build_export_lines(
        "brave", args, prefs_path, prefs, known_prefixes=KNOWN_SETTINGS
    )


def register(subparsers: argparse._SubParsersAction, profile_args) -> None:
    _base.register("brave", subparsers, profile_args)
