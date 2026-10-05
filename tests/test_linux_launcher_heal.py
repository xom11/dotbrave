"""Tests for the Linux PWA launcher heal.

A Brave started on a throwaway --user-data-dir obeys the managed [pwa]
policy too, and overwrites the real profile's launchers with its own data
dir in Exec. The heal is a systemd user path unit plus a shell script that
puts them back. The pure builders run anywhere; the script itself runs under
a real /bin/sh against a tmp applications dir; install/remove record the
systemctl calls instead of making them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave import pwa
from dotbrave._base import pwa as base

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the launcher heal is Linux-only"
)

APP = "brave-aaaabbbbccccddddeeeeffffgggghhhh-Default.desktop"
GOOD = (
    "[Desktop Entry]\n"
    "Name=Claude\n"
    "Exec=brave --profile-directory=Default --app-id=aaaabbbbccccddddeeeeffffgggghhhh\n"
)
HIJACKED = (
    "[Desktop Entry]\n"
    "Name=claude.ai\n"
    "Exec=brave --user-data-dir=/tmp/scratch/prof --profile-directory=Default"
    " --app-id=aaaabbbbccccddddeeeeffffgggghhhh\n"
)


@pytest.fixture
def paths(tmp_path: Path) -> base.LinuxHealPaths:
    p = base.LinuxHealPaths(
        apps_dir=tmp_path / "share" / "applications",
        state_dir=tmp_path / "state" / "dotbrave",
        unit_dir=tmp_path / "config" / "systemd" / "user",
    )
    p.apps_dir.mkdir(parents=True)
    return p


def _run_heal(paths: base.LinuxHealPaths) -> None:
    script = paths.state_dir.parent / "heal.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(base.build_linux_heal_script(paths))
    subprocess.run(["/bin/sh", str(script)], check=True)


def _log(paths: base.LinuxHealPaths) -> str:
    return paths.log.read_text() if paths.log.exists() else ""


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_paths_follow_absolute_xdg_vars_and_ignore_relative_ones() -> None:
    p = base.linux_heal_paths({
        "HOME": "/home/u",
        "XDG_DATA_HOME": "/data",
        "XDG_STATE_HOME": "relative/state",
    })
    assert p.apps_dir == Path("/data/applications")
    assert p.state_dir == Path("/home/u/.local/state/dotbrave")
    assert p.unit_dir == Path("/home/u/.config/systemd/user")


def test_path_unit_watches_the_applications_dir(paths) -> None:
    unit = base.build_systemd_path_unit(paths)
    assert f"PathChanged={paths.apps_dir}\n" in unit
    assert "Unit=dotbrave-pwa-heal.service\n" in unit
    assert "WantedBy=default.target\n" in unit


def test_service_unit_runs_the_script_once_per_trigger(paths) -> None:
    unit = base.build_systemd_service_unit(paths)
    assert "Type=oneshot\n" in unit
    assert f'ExecStart=/bin/sh "{paths.script}"\n' in unit


def test_service_unit_survives_a_burst_of_triggers(paths) -> None:
    """The default start limit killed the service, and the path unit with
    it, while a throwaway profile was writing a dozen launchers."""
    assert "StartLimitIntervalSec=0\n" in base.build_systemd_service_unit(paths)


def test_timer_reruns_the_heal_as_a_safety_net() -> None:
    unit = base.build_systemd_timer_unit()
    assert "OnUnitInactiveSec=60\n" in unit
    assert "Unit=dotbrave-pwa-heal.service\n" in unit
    assert "WantedBy=timers.target\n" in unit


def test_unit_values_escape_the_specifier_character(tmp_path: Path) -> None:
    p = base.LinuxHealPaths(tmp_path / "a%b", tmp_path / "s", tmp_path / "u")
    assert "a%%b" in base.build_systemd_path_unit(p)


def test_script_inlines_its_inputs_under_a_single_shebang(paths) -> None:
    text = base.build_linux_heal_script(paths)
    assert text.count("#!/bin/sh") == 1
    assert f"APPS={paths.apps_dir}\n" in text
    assert "export APPS SNAP LOG\n" in text


# ---------------------------------------------------------------------------
# The script itself
# ---------------------------------------------------------------------------


@linux_only
def test_good_launcher_is_snapshotted(paths) -> None:
    (paths.apps_dir / APP).write_text(GOOD)
    _run_heal(paths)
    assert (paths.snapshot_dir / APP).read_text() == GOOD
    assert _log(paths) == ""


@linux_only
def test_hijacked_launcher_is_restored_from_the_snapshot(paths) -> None:
    (paths.apps_dir / APP).write_text(GOOD)
    _run_heal(paths)
    (paths.apps_dir / APP).write_text(HIJACKED)
    _run_heal(paths)
    assert (paths.apps_dir / APP).read_text() == GOOD
    assert f"restored {APP}" in _log(paths)


@linux_only
def test_first_run_strips_an_unknown_hijacked_launcher_instead_of_deleting(
    paths,
) -> None:
    """Before any launcher was seen good, a hijacked one may be the only
    launcher a real app has."""
    quoted = HIJACKED.replace(
        "--user-data-dir=/tmp/scratch/prof", '"--user-data-dir=/tmp/a b/prof"'
    )
    other = APP.replace("aaaa", "zzzz")
    (paths.apps_dir / APP).write_text(HIJACKED)
    (paths.apps_dir / other).write_text(quoted)
    _run_heal(paths)
    for name in (APP, other):
        text = (paths.apps_dir / name).read_text()
        assert "--user-data-dir" not in text
        assert "--profile-directory=Default --app-id=" in text
        assert f"stripped {name}" in _log(paths)


@linux_only
def test_later_unknown_hijacked_launcher_is_deleted_with_its_icons(paths) -> None:
    """Once the heal has run, every real launcher passed through it good, so
    one with no snapshot is an app id only the throwaway profile has."""
    (paths.apps_dir / APP).write_text(GOOD)
    _run_heal(paths)

    extra = APP.replace("aaaa", "zzzz")
    icon = (paths.apps_dir.parent / "icons" / "hicolor" / "48x48" / "apps"
            / extra.replace(".desktop", ".png"))
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"png")
    (paths.apps_dir / extra).write_text(HIJACKED.replace("aaaa", "zzzz"))
    _run_heal(paths)

    assert not (paths.apps_dir / extra).exists()
    assert not icon.exists()
    assert f"removed {extra}" in _log(paths)
    assert (paths.apps_dir / APP).read_text() == GOOD


@linux_only
def test_launchers_without_an_app_id_are_left_alone(paths) -> None:
    browser = "brave-browser.desktop"
    text = "[Desktop Entry]\nExec=brave --user-data-dir=/x %U\n"
    (paths.apps_dir / browser).write_text(text)
    _run_heal(paths)
    assert (paths.apps_dir / browser).read_text() == text


@linux_only
def test_launcher_briefly_missing_is_still_restored_not_deleted(paths) -> None:
    """Brave rewrites a launcher by deleting and recreating it. A run in that
    gap used to forget the snapshot, and the next run deleted the real app's
    hijacked launcher as a throwaway-only id -- measured on a real profile."""
    (paths.apps_dir / APP).write_text(GOOD)
    _run_heal(paths)
    (paths.apps_dir / APP).unlink()
    _run_heal(paths)
    (paths.apps_dir / APP).write_text(HIJACKED)
    _run_heal(paths)
    assert (paths.apps_dir / APP).read_text() == GOOD
    assert "removed" not in _log(paths)


@linux_only
def test_second_run_writes_nothing(paths) -> None:
    """The path unit fires on the heal's own writes; a run that changes
    nothing is what stops that from looping."""
    (paths.apps_dir / APP).write_text(GOOD)
    _run_heal(paths)
    (paths.apps_dir / APP).write_text(HIJACKED)
    _run_heal(paths)
    stamps = {p: p.stat().st_mtime_ns for p in
              (paths.apps_dir / APP, paths.snapshot_dir / APP, paths.log)}
    log = _log(paths)
    _run_heal(paths)
    assert {p: p.stat().st_mtime_ns for p in stamps} == stamps
    assert _log(paths) == log


# ---------------------------------------------------------------------------
# install / remove
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.rc, "", "")


def test_install_writes_files_and_starts_the_watch(paths, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(base.subprocess, "run", rec)
    base.install_linux_launcher_heal(paths)

    assert base.linux_heal_current(paths)
    assert os.access(paths.script, os.X_OK)
    assert rec.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now",
         "dotbrave-pwa-heal.path", "dotbrave-pwa-heal.timer"],
        ["systemctl", "--user", "start", "dotbrave-pwa-heal.service"],
    ]


def test_install_without_a_user_manager_warns_but_keeps_the_files(
    paths, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(base.subprocess, "run", _Recorder(rc=1))
    base.install_linux_launcher_heal(paths)
    assert base.linux_heal_current(paths)
    assert "not running" in capsys.readouterr().err


def test_remove_stops_the_watch_and_deletes_only_its_own_files(
    paths, monkeypatch,
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(base.subprocess, "run", rec)
    base.install_linux_launcher_heal(paths)
    paths.snapshot_dir.mkdir(parents=True)
    (paths.snapshot_dir / APP).write_text(GOOD)
    (paths.apps_dir / APP).write_text(GOOD)

    base.remove_linux_launcher_heal(paths)

    assert ["systemctl", "--user", "disable", "--now",
            "dotbrave-pwa-heal.path", "dotbrave-pwa-heal.timer"] in rec.calls
    assert not base.linux_heal_present(paths)
    assert not paths.state_dir.exists()
    assert (paths.apps_dir / APP).read_text() == GOOD


# ---------------------------------------------------------------------------
# Through `apply`
# ---------------------------------------------------------------------------


class _FakeHeal:
    def __init__(self, current: bool, present: bool) -> None:
        self.is_current = current
        self.is_present = present
        self.installed = 0
        self.removed = 0

    def hooks(self) -> base.LauncherHeal:
        def install() -> None:
            self.installed += 1

        def remove() -> None:
            self.removed += 1

        return base.LauncherHeal(
            current=lambda: self.is_current,
            present=lambda: self.is_present,
            install=install,
            remove=remove,
        )


@pytest.fixture
def apply_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if sys.platform == "win32":
        pytest.skip("the launcher heal is Linux-only")
    root = tmp_path / "root"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Preferences").write_text(json.dumps({"some": "thing"}))
    policy = tmp_path / "policy" / "dotbrave-pwa.json"
    monkeypatch.setattr(pwa, "POLICY_FILE", policy)
    writes: list[list[dict]] = []

    def fake_write(entries: list[dict]) -> None:
        writes.append(entries)
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_bytes(pwa._build_policy_payload(entries))

    monkeypatch.setattr(pwa, "_sudo_write_policy", fake_write)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if list(cmd[:2]) in (["sudo", "-v"], ["sudo", "-n"]):
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *args, **kwargs)

    from dotbrave._base import orchestrator as orch
    monkeypatch.setattr(orch.subprocess, "run", fake_run)

    def apply(urls: list[str], heal: _FakeHeal) -> None:
        monkeypatch.setattr(pwa, "_launcher_heal", heal.hooks)
        config = tmp_path / "brave.toml"
        config.write_text(
            "[pwa]\nurls = [" + ", ".join(json.dumps(u) for u in urls) + "]\n"
        )
        brave_pkg.cmd_apply(argparse.Namespace(
            profile_root=root, profile="Default", config=config, dry_run=False,
        ))

    return apply, writes


def test_apply_installs_the_heal_with_the_policy(apply_env, capsys) -> None:
    apply, writes = apply_env
    heal = _FakeHeal(current=False, present=False)
    apply(["https://claude.ai/"], heal)
    assert "+ launcher heal" in capsys.readouterr().out
    assert len(writes) == 1
    assert heal.installed == 1


def test_matching_policy_still_gets_the_heal_without_rewriting_it(
    apply_env,
) -> None:
    """A machine set up before the heal existed has a policy that already
    matches; it must get the heal on the next apply, and that apply has no
    reason to touch the policy file."""
    apply, writes = apply_env
    apply(["https://claude.ai/"], _FakeHeal(current=True, present=True))
    writes.clear()

    heal = _FakeHeal(current=False, present=False)
    apply(["https://claude.ai/"], heal)
    assert writes == []
    assert heal.installed == 1


def test_current_heal_and_policy_is_no_change(apply_env, capsys) -> None:
    apply, _ = apply_env
    apply(["https://claude.ai/"], _FakeHeal(current=True, present=True))
    capsys.readouterr()
    apply(["https://claude.ai/"], _FakeHeal(current=True, present=True))
    assert "no changes" in capsys.readouterr().out


def test_empty_table_removes_the_heal(apply_env, capsys) -> None:
    apply, _ = apply_env
    apply(["https://claude.ai/"], _FakeHeal(current=True, present=True))
    capsys.readouterr()
    heal = _FakeHeal(current=True, present=True)
    apply([], heal)
    assert "- launcher heal" in capsys.readouterr().out
    assert heal.removed == 1
    assert heal.installed == 0
