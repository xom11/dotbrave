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
