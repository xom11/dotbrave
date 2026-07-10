"""Tests for the `<browser> restore` subcommand.

Driven via Brave (the same orchestrator code paths cover Edge / Vivaldi
since `cmd_restore` lives in `_base/orchestrator.py`).  Each test
synthesizes a profile + a backup file or two, calls `cmd_restore` in-
process with a fake `Namespace`, and asserts the resulting filesystem
state.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _restore(
    profile_root: Path,
    *,
    from_path: str | None = None,
    list_only: bool = False,
    dry_run: bool = False,
) -> None:
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        from_path=from_path,
        list=list_only,
        dry_run=dry_run,
    )
    brave_pkg.cmd_restore(args)


def _run_cli(profile_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable, "-m", "dotbrave",
            "--profile-root", str(profile_root), *extra,
        ],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def profile_with_backups(tmp_path: Path) -> tuple[Path, list[Path]]:
    """Profile root with a current Preferences plus three timestamped
    backups (oldest -> newest by mtime).  Includes both shortcuts and
    settings sidecars so the clear-on-restore path can be exercised."""
    profile = tmp_path / "Default"
    profile.mkdir()

    current = {"version": "current", "marker": "now"}
    (profile / "Preferences").write_text(json.dumps(current))

    # Sidecars that should get cleared on restore.
    (profile / "Preferences.dotbrave.shortcuts.json").write_text(
        json.dumps({"managed_ids": ["35012"]})
    )
    (profile / "Preferences.dotbrave.settings.json").write_text(
        json.dumps({"managed_keys": ["brave.tabs.vertical_tabs_enabled"]})
    )

    backups: list[Path] = []
    for i, marker in enumerate(("oldest", "middle", "newest")):
        bak = profile / f"Preferences.bak.20260101-00000{i}"
        bak.write_text(json.dumps({"version": marker, "marker": marker}))
        # Force ascending mtimes so newest sorts last regardless of
        # filesystem timestamp granularity.
        os.utime(bak, (1_000_000 + i, 1_000_000 + i))
        backups.append(bak)
        # Tiny gap so test_list_orders_by_mtime sees distinct mtimes
        # even on coarse-clock filesystems.
        time.sleep(0.01)
    return tmp_path, backups


def test_restore_picks_most_recent_backup(
    profile_with_backups: tuple[Path, list[Path]], monkeypatch
) -> None:
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    profile_root, backups = profile_with_backups
    newest = backups[-1]

    _restore(profile_root)

    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    assert prefs["marker"] == "newest"
    assert prefs == json.loads(newest.read_text())


def test_restore_clears_sidecars(
    profile_with_backups: tuple[Path, list[Path]], monkeypatch
) -> None:
    """Sidecars should be removed so the next apply doesn't try to
    'remove' keys that the restore put back."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    profile_root, _ = profile_with_backups

    _restore(profile_root)

    profile_dir = profile_root / "Default"
    assert not (profile_dir / "Preferences.dotbrave.shortcuts.json").exists()
    assert not (profile_dir / "Preferences.dotbrave.settings.json").exists()


def test_restore_from_specific_backup(
    profile_with_backups: tuple[Path, list[Path]], monkeypatch
) -> None:
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    profile_root, backups = profile_with_backups
    middle = backups[1]

    _restore(profile_root, from_path=str(middle))

    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    assert prefs["marker"] == "middle"


def test_restore_from_missing_backup_errors(
    profile_with_backups: tuple[Path, list[Path]], tmp_path: Path
) -> None:
    profile_root, _ = profile_with_backups
    bogus = tmp_path / "does-not-exist.bak"
    with pytest.raises(SystemExit, match="backup not found"):
        _restore(profile_root, from_path=str(bogus))


def test_restore_no_backups_errors(tmp_path: Path) -> None:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")
    with pytest.raises(SystemExit, match="no backups found"):
        _restore(tmp_path)


def test_restore_dry_run_does_not_write(
    profile_with_backups: tuple[Path, list[Path]], monkeypatch
) -> None:
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    profile_root, _ = profile_with_backups

    prefs_path = profile_root / "Default" / "Preferences"
    before = prefs_path.read_bytes()
    sidecar_before = (
        profile_root / "Default" / "Preferences.dotbrave.shortcuts.json"
    ).read_bytes()

    _restore(profile_root, dry_run=True)

    assert prefs_path.read_bytes() == before
    assert (
        profile_root / "Default" / "Preferences.dotbrave.shortcuts.json"
    ).read_bytes() == sidecar_before


def test_restore_running_browser_closes_normally_and_restarts(
    profile_with_backups: tuple[Path, list[Path]], monkeypatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS, "close_and_wait", lambda: calls.append("close")
    )
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(
        brave_pkg,
        "restart_brave",
        lambda cmd: calls.append(("restart", cmd)) or cmd,
    )
    profile_root, _ = profile_with_backups

    _restore(profile_root)

    assert calls == ["close", ("restart", ["brave"])]


def test_list_lists_backups_newest_first(
    profile_with_backups: tuple[Path, list[Path]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_root, _ = profile_with_backups
    _restore(profile_root, list_only=True)
    out = capsys.readouterr().out
    # All three backup filenames are present
    assert "Preferences.bak.20260101-000000" in out
    assert "Preferences.bak.20260101-000001" in out
    assert "Preferences.bak.20260101-000002" in out
    # newest (index 2) appears before oldest (index 0)
    assert out.index("000002") < out.index("000000")


def test_list_with_no_backups_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")
    _restore(tmp_path, list_only=True)
    assert "no backups found" in capsys.readouterr().out


def test_cli_restore_action_is_gone() -> None:
    """The standalone `restore` action was folded into `apply --undo`."""
    profile = REPO_ROOT  # arbitrary path; argparse rejects before reading it
    r = _run_cli(profile, "restore", "--help")
    assert r.returncode != 0
    assert "invalid choice" in r.stderr


def test_apply_undo_restores_most_recent_backup(
    profile_with_backups: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """`apply --undo` routes to the restore engine: newest backup wins."""
    profile_root, backups = profile_with_backups
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=None,
        undo=True,
        dry_run=False,
        channel="stable",
    )
    brave_pkg.cmd_apply(args)

    prefs = (profile_root / "Default" / "Preferences").read_text()
    newest = backups[-1].read_text()
    assert prefs == newest


def test_apply_undo_rejects_config_argument(tmp_path: Path) -> None:
    args = argparse.Namespace(
        profile_root=tmp_path,
        profile="Default",
        config="some.toml",
        undo=True,
        dry_run=False,
        channel="stable",
    )
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(args)
    assert "--undo" in str(exc.value)


def test_apply_without_config_or_undo_errors(tmp_path: Path) -> None:
    args = argparse.Namespace(
        profile_root=tmp_path,
        profile="Default",
        config=None,
        undo=False,
        dry_run=False,
        channel="stable",
    )
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(args)
    assert "CONFIG" in str(exc.value)
