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
