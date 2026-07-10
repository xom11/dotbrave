"""User-facing help text regression coverage.

These tests execute the installed CLI surface instead of introspecting
argparse internals. Help is part of dotbrave's discoverability contract:
it must report only capabilities the tool actually has -- and the tool
has exactly two actions, `apply` and `export`.
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
    assert "apply --undo" in out
    assert "export -o" in out


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


def test_apply_help_covers_undo() -> None:
    apply_help = _help("apply")
    assert "--undo" in apply_help
    assert "backup" in apply_help


def test_export_help_covers_settings_sources_and_limits() -> None:
    export = _help("export")
    assert "well-known" in export
    assert "--snapshot" in export
    assert "MAC-protected" in export
    # Shortcut-name discovery lives here now.
    assert "-a" in export


def test_removed_actions_are_rejected() -> None:
    """The old action tree is gone: exactly `apply` and `export` remain."""
    for action in ("init", "restore", "shortcuts", "settings", "pwa"):
        result = _run(action, "--help")
        assert result.returncode != 0, f"{action} should be gone"
        assert "invalid choice" in result.stderr
