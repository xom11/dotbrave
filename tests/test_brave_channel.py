"""End-to-end coverage for ``brave --channel`` CLI integration.

Companion to ``test_platform.py`` (which verifies the path/process
factories in isolation).  These tests exercise the full argparse path
plus the post-parse normalizer that fills in ``--profile-root`` from
``--channel`` when omitted.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "dotbrave", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _isolated_home_env(tmp_path: Path) -> dict[str, str]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_local = tmp_path / "local"
    fake_local.mkdir()
    return {
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "LOCALAPPDATA": str(fake_local),
    }


def test_channel_flag_appears_in_help() -> None:
    r = _run_cli("--help")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "--channel" in out
    for ch in ("stable", "beta", "nightly"):
        assert ch in out


def test_invalid_channel_rejected() -> None:
    r = _run_cli("--channel", "dev", "export")
    assert r.returncode != 0
    assert "invalid choice" in (r.stdout + r.stderr).lower()


def test_channel_beta_resolves_default_profile_root(tmp_path: Path) -> None:
    """Without --profile-root, --channel beta must auto-resolve to the
    Brave-Browser-Beta directory and surface its absence as a
    'Preferences not found' error pointing into that directory."""
    env = _isolated_home_env(tmp_path)
    r = _run_cli("--channel", "beta", "export", env_overrides=env)
    assert r.returncode != 0, r.stdout
    msg = r.stdout + r.stderr
    # The resolver picked the Beta-suffixed path.
    assert "Brave-Browser-Beta" in msg or "Brave-Browser-beta" in msg


def test_channel_default_is_stable(tmp_path: Path) -> None:
    """Omitting --channel must resolve to the stable profile path."""
    env = _isolated_home_env(tmp_path)
    r = _run_cli("export", env_overrides=env)
    assert r.returncode != 0
    msg = r.stdout + r.stderr
    assert "Brave-Browser" in msg
    assert "Brave-Browser-Beta" not in msg
    assert "Brave-Browser-Nightly" not in msg


def test_explicit_profile_root_wins_over_channel(tmp_path: Path) -> None:
    """If the user passes --profile-root, the resolver must not override
    it from the channel default."""
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        '{"protection":{"macs":{"homepage":"DEAD"}}}'
    )
    r = _run_cli(
        "--channel", "nightly", "--profile-root", str(tmp_path),
        "export",
    )
    assert r.returncode == 0, r.stderr
    # The export header names the source profile: the explicit root, not
    # the nightly default.
    assert f"Source profile: {tmp_path}" in r.stdout
