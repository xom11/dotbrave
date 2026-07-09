"""Smoke tests against the real on-disk Brave profile (read-only).

Skipped automatically if no Brave profile is present (CI, fresh machine,
non-supported OS). Never modifies Preferences — only invokes `list`,
`dump`, and `apply --dry-run`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dotbrave.browser import DEFAULT_PROFILE_ROOT

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CFG = REPO_ROOT / "examples" / "all.toml"

requires_brave_profile = pytest.mark.skipif(
    DEFAULT_PROFILE_ROOT is None
    or not (DEFAULT_PROFILE_ROOT / "Default" / "Preferences").exists(),
    reason="no real Brave profile present at the platform default",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "dotbrave", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_help_does_not_crash() -> None:
    """Even on Windows / unsupported OSes, --help must work."""
    r = _run("--help")
    assert r.returncode == 0
    assert "brave" in r.stdout.lower()


def test_root_help_lists_actions() -> None:
    r = _run("--help")
    assert r.returncode == 0
    # Top-level apply + the two read-only inspection sub-namespaces
    assert "apply" in r.stdout
    assert "shortcuts" in r.stdout
    assert "settings" in r.stdout


def test_list_returns_known_commands() -> None:
    r = _run("shortcuts", "list", "focus")
    # `list` does NOT need a profile; it just prints the static mapping
    assert r.returncode == 0, r.stderr
    assert "focus_location" in r.stdout


@requires_brave_profile
def test_dump_real_profile_succeeds() -> None:
    r = _run("shortcuts", "dump")
    assert r.returncode == 0, r.stderr
    # `dump` always emits a [shortcuts] header
    assert "[shortcuts]" in r.stdout


@requires_brave_profile
def test_dry_run_apply_real_profile_does_not_write() -> None:
    assert EXAMPLE_CFG.exists(), "examples/all.toml is part of the repo"
    real_prefs = DEFAULT_PROFILE_ROOT / "Default" / "Preferences"
    before = real_prefs.read_bytes()
    r = _run("apply", str(EXAMPLE_CFG), "--dry-run")
    # Real Brave may be running — that's fine for a dry-run.
    assert r.returncode == 0, r.stderr
    after = real_prefs.read_bytes()
    assert before == after, "dry-run must not touch the real Preferences file"
