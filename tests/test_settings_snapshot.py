"""Tests for `dotbrave settings snapshot` and the snapshot-diff export.

The snapshot is a JSON sidecar (`Preferences.dotbrave.settings-snapshot.json`)
capturing Preferences + Secure Preferences so `export` can emit a [settings]
block of keys changed via the browser UI since the snapshot was taken.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotbrave import settings as brave_settings
from dotbrave._base import settings as base_settings
from dotbrave._base.utils import find_preferences, load_prefs


def _args(profile_root: Path, **extra) -> argparse.Namespace:
    ns = argparse.Namespace(
        profile_root=profile_root, profile="Default", channel="stable"
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def _snapshot_path(profile_root: Path) -> Path:
    return (
        profile_root / "Default" / "Preferences.dotbrave.settings-snapshot.json"
    )


# ---------------------------------------------------------------------------
# settings snapshot / --clear
# ---------------------------------------------------------------------------

def test_snapshot_writes_sidecar(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    snap = _snapshot_path(fake_settings_profile_root)
    assert snap.exists()
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["prefs"]["homepage"] == "https://existing-home.example"
    # No sibling Secure Preferences in the fixture -> stored as {}.
    assert data["secure_prefs"] == {}
    assert isinstance(data["created"], str) and data["created"]
    assert "snapshot saved" in capsys.readouterr().out


def test_snapshot_captures_secure_preferences(
    fake_settings_profile_root: Path,
) -> None:
    secure = fake_settings_profile_root / "Default" / "Secure Preferences"
    secure.write_text(json.dumps({"homepage": "https://secure.example"}))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    data = json.loads(
        _snapshot_path(fake_settings_profile_root).read_text(encoding="utf-8")
    )
    assert data["secure_prefs"]["homepage"] == "https://secure.example"


def test_snapshot_overwrites_previous(
    fake_settings_profile_root: Path,
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    prefs["homepage"] = "https://changed.example"
    prefs_path.write_text(json.dumps(prefs))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    data = json.loads(
        _snapshot_path(fake_settings_profile_root).read_text(encoding="utf-8")
    )
    assert data["prefs"]["homepage"] == "https://changed.example"


def test_snapshot_clear_removes_sidecar(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    assert _snapshot_path(fake_settings_profile_root).exists()
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=True))
    assert not _snapshot_path(fake_settings_profile_root).exists()
    assert "removed" in capsys.readouterr().out


def test_snapshot_clear_without_snapshot_is_noop(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=True))
    assert "no snapshot" in capsys.readouterr().out


def test_load_snapshot_absent_returns_none(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    assert base_settings._load_snapshot(prefs_path) is None


def test_load_snapshot_corrupt_exits(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._snapshot_file(prefs_path).write_text("{not json")
    with pytest.raises(SystemExit) as exc:
        base_settings._load_snapshot(prefs_path)
    assert "snapshot" in str(exc.value)


def test_load_snapshot_malformed_payload_exits(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._snapshot_file(prefs_path).write_text(json.dumps({"prefs": 42}))
    with pytest.raises(SystemExit):
        base_settings._load_snapshot(prefs_path)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_snapshot_subcommand_is_registered() -> None:
    from dotbrave.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["settings", "snapshot"])
    assert getattr(args, "_needs_profile", False) is True
    assert args.clear is False
    args = parser.parse_args(["settings", "snapshot", "--clear"])
    assert args.clear is True


# ---------------------------------------------------------------------------
# _walk_leaf_diffs / _is_volatile
# ---------------------------------------------------------------------------

def _diffs(old: dict, new: dict) -> list[tuple[tuple[str, ...], object, object]]:
    return list(base_settings._walk_leaf_diffs(old, new))


def test_walk_identical_yields_nothing() -> None:
    prefs = {"a": {"b": 1}, "c": [1, 2]}
    assert _diffs(prefs, json.loads(json.dumps(prefs))) == []


def test_walk_changed_scalar_leaf() -> None:
    assert _diffs({"a": {"b": 1}}, {"a": {"b": 2}}) == [(("a", "b"), 1, 2)]


def test_walk_added_nested_subtree_reports_leaves() -> None:
    got = _diffs({}, {"brave": {"tabs": {"vertical_tabs_enabled": True}}})
    assert got == [
        (("brave", "tabs", "vertical_tabs_enabled"), base_settings._MISSING, True)
    ]


def test_walk_removed_leaf_reports_missing_new() -> None:
    got = _diffs({"a": {"b": 1}}, {"a": {}})
    assert got == [(("a", "b"), 1, base_settings._MISSING)]


def test_walk_list_change_is_one_leaf() -> None:
    assert _diffs({"a": [1, 2]}, {"a": [1, 3]}) == [(("a",), [1, 2], [1, 3])]


def test_walk_dict_replaced_by_scalar_reports_at_that_path() -> None:
    assert _diffs({"a": {"b": 1}}, {"a": 5}) == [(("a",), {"b": 1}, 5)]


def test_is_volatile_prefix_and_leaf_names() -> None:
    assert base_settings._is_volatile(("protection", "macs", "homepage"))
    assert base_settings._is_volatile(("sessions", "event_log"))
    assert base_settings._is_volatile(("browser", "window_placement", "left"))
    assert base_settings._is_volatile(
        ("profile", "content_settings", "x", "last_modified")
    )
    assert not base_settings._is_volatile(
        ("brave", "tabs", "vertical_tabs_enabled")
    )
    # `session` (singular) holds real user settings -- must NOT be filtered.
    assert not base_settings._is_volatile(("session", "restore_on_startup"))
    # Churn observed on a real, running profile.
    assert base_settings._is_volatile(
        ("profile", "content_settings", "exceptions", "site_engagement",
         "https://github.com:443,*", "setting", "rawScore")
    )
    assert base_settings._is_volatile(
        ("web_apps", "daily_metrics", "https://claude.ai/",
         "background_duration_sec")
    )
    assert base_settings._is_volatile(
        ("account_values", "bookmark_bar", "show_on_all_tabs")
    )
    # ...but ordinary content-settings exceptions stay exportable.
    assert not base_settings._is_volatile(
        ("profile", "content_settings", "exceptions", "notifications",
         "https://example.com:443,*", "setting")
    )


# ---------------------------------------------------------------------------
# build_export_lines
# ---------------------------------------------------------------------------

def _snapshot_then_mutate(
    profile_root: Path, mutate
) -> tuple[Path, dict]:
    """Take a snapshot, apply `mutate(prefs)` to Preferences, reload."""
    brave_settings.cmd_snapshot(_args(profile_root, clear=False))
    prefs_path = find_preferences(profile_root, "Default")
    prefs = load_prefs(prefs_path)
    mutate(prefs)
    prefs_path.write_text(json.dumps(prefs))
    return prefs_path, load_prefs(prefs_path)


def test_export_lines_known_settings_without_snapshot(
    fake_settings_profile_root: Path,
) -> None:
    """No snapshot needed: allowlisted keys present in Preferences are
    exported at their current values; MAC-protected ones demote to
    comments; no snapshot section appears."""
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    args = _args(fake_settings_profile_root)
    lines = brave_settings.build_export_lines(args, prefs_path, prefs)
    assert lines is not None and lines[0] == "[settings]"
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is False
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is False
    # browser.show_home_button is allowlisted but MAC-protected here.
    assert "browser.show_home_button" not in doc["settings"]
    assert "MAC-protected" in body and "browser.show_home_button" in body
    assert "changed since snapshot" not in body


def test_known_prefix_metrics_still_filtered(
    fake_settings_profile_root: Path,
) -> None:
    """Volatile leaves under an allowlisted prefix stay hidden."""
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    prefs["bookmark_bar"]["last_visit"] = "13381990"
    prefs_path.write_text(json.dumps(prefs))
    prefs = load_prefs(prefs_path)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    assert "last_visit" not in "\n".join(lines)


def test_export_lines_diff_since_snapshot(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    def mutate(prefs: dict) -> None:
        prefs["brave"]["tabs"]["vertical_tabs_enabled"] = True   # changed
        prefs["omnibox"] = {"prevent_url_elisions": True}        # added subtree
        prefs["sessions"] = {"event_log": [1, 2, 3]}             # volatile: hidden

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    assert lines is not None and lines[0] == "[settings]"
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is True
    assert doc["settings"]["omnibox.prevent_url_elisions"] is True
    assert "sessions" not in body
    assert "changed since snapshot" in body


def test_export_lines_includes_managed_keys(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    # Simulate a prior apply that manages one key.
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )

    def mutate(prefs: dict) -> None:
        prefs["brave"]["tabs"]["vertical_tabs_enabled"] = True

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    doc = tomllib.loads("\n".join(lines))
    # Managed key present at its current value + the UI-changed key.
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is False
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is True
    assert "# currently managed by dotbrave" in "\n".join(lines)


def test_export_lines_managed_key_not_duplicated_when_changed(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )

    def mutate(prefs: dict) -> None:
        prefs["bookmark_bar"]["show_tab_groups"] = True  # managed AND changed

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)  # would raise on a duplicate TOML key
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is True


def test_export_lines_zero_diff_still_emits_managed_block(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )
    prefs_path, prefs = _snapshot_then_mutate(
        fake_settings_profile_root, lambda prefs: None
    )
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    assert "no changes since snapshot" in body
    doc = tomllib.loads(body)
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is False


def test_export_lines_mac_protected_change_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        # browser.show_home_button is MAC-protected in the fixture.
        prefs["browser"]["show_home_button"] = False

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "browser.show_home_button" not in doc.get("settings", {})
    assert "MAC-protected" in body
    assert "browser.show_home_button" in body


def test_export_lines_secure_prefs_change_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    secure = fake_settings_profile_root / "Default" / "Secure Preferences"
    secure.write_text(json.dumps({"homepage": "https://old.example"}))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    secure.write_text(json.dumps({"homepage": "https://new.example"}))

    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "homepage" not in doc.get("settings", {})
    assert "MAC-protected" in body and "homepage" in body


def test_export_lines_removed_leaf_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        del prefs["some"]["unrelated"]

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "some.unrelated" not in doc.get("settings", {})
    assert "removed since the snapshot" in body


def test_export_lines_unrepresentable_value_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        prefs["weird"] = None  # TOML has no null

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "weird" not in doc.get("settings", {})
    assert "not representable" in body
