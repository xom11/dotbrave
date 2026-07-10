"""User-facing help text regression coverage.

These tests execute the installed CLI surface instead of introspecting
argparse internals. Help is part of dotbrave's discoverability contract:
it must report only capabilities the tool actually has.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "dotbrave", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _help(*args: str) -> str:
    result = _run(*args, "--help")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_root_help_explains_capabilities_and_workflow() -> None:
    out = _help()
    assert "Brave" in out and "[shortcuts] [settings] [pwa]" in out
    assert "--channel" in out
    assert "Typical workflow" in out
    assert "apply --dry-run" in out
    assert "restore" in out


def test_help_advertises_automatic_live_apply() -> None:
    apply_help = _help("apply")
    assert "live apply" in apply_help.lower()
    assert "--kill-browser" not in apply_help
    assert "--live-port" not in apply_help
    assert _run("launch", "--help").returncode != 0


def test_profile_flags_work_before_and_after_the_action() -> None:
    apply_help = _help("apply")
    root_help = _help()
    for flag in ("--channel", "--profile-root", "--profile"):
        assert flag in root_help
        assert flag in apply_help


def test_removed_apply_flags_are_rejected() -> None:
    removed = [
        ("--kill-browser",),
        ("--live-port", "9333"),
    ]
    for option in removed:
        result = _run("apply", *option, "missing.toml")
        assert result.returncode != 0
        assert "unrecognized arguments" in result.stderr


def test_export_and_restore_help_state_deliberate_limits() -> None:
    export = _help("export")
    restore = _help("restore")
    # [settings] needs a `settings snapshot` baseline; no snapshot, no block.
    assert "settings\nsnapshot`" in export
    assert "[settings] block is omitted" in export
    assert "[pwa] policy is not restored" in restore


def test_namespace_help_explains_specialized_discovery() -> None:
    shortcuts = _help("shortcuts")
    settings = _help("settings")
    pwa = _help("pwa")
    assert "Chromium KeyEvent codes" in shortcuts
    assert "MAC-protected" in settings
    assert "managed policy" in pwa
