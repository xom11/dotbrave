"""Clean-error-message regression tests.

Covers:
- ``_load_toml`` exits cleanly on malformed local TOML and missing files
  (issue #13).
- The sudo preflight in ``cmd_apply`` exits cleanly on systems without
  sudo, instead of letting ``FileNotFoundError`` bubble (issue #14).

All tests are pure-logic and run offline -- no Brave install required.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from dotbrave._base import orchestrator as orch
from dotbrave._base.utils import Plan


# ---------------------------------------------------------------------------
# #13: TOML parse / file errors
# ---------------------------------------------------------------------------


def test_load_toml_malformed_file_exits_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("not = valid = toml\n")
    with pytest.raises(SystemExit, match=r"invalid TOML at .*bad\.toml"):
        orch._load_toml(bad)


def test_load_toml_missing_file_exits_cleanly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(SystemExit, match="config file not found"):
        orch._load_toml(missing)


def test_load_toml_well_formed_passes(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text('[settings]\n"foo.bar" = true\n')
    assert orch._load_toml(good) == {"settings": {"foo.bar": True}}


# ---------------------------------------------------------------------------
# #14: sudo preflight on systems without sudo
# ---------------------------------------------------------------------------


def _build_pwa_only_plans(_prefs_path, _prefs, _doc):
    """Stand-in build_plans_fn that yields a single non-empty pwa-shaped
    plan -- enough to make the orchestrator hit the sudo preflight."""
    return [
        Plan(
            namespace="pwa",
            diff_lines=["  + https://example.com/"],
            apply_fn=lambda _: None,
            verify_fn=lambda _: None,
            external_apply_fn=lambda: None,
        )
    ]


def _make_args(profile_root: Path, config: Path) -> argparse.Namespace:
    return argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(config),
        dry_run=False,
        allow_http=False,
        expect_sha256=None,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="Linux/macOS-only path; Windows uses IsUserAnAdmin"
)
def test_sudo_preflight_handles_missing_sudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a system where ``sudo`` is not on PATH, the cached check
    raises FileNotFoundError. The orchestrator must surface a clean
    error, not a traceback.
    """
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")

    cfg = tmp_path / "x.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com/"]\n')

    def boom(cmd, *args, **kwargs):
        # Both the cached `-n true` and the interactive `-v` go through
        # subprocess.run; either being unavailable is the thing we test.
        raise FileNotFoundError(2, "No such file or directory: 'sudo'")

    monkeypatch.setattr(orch.subprocess, "run", boom)

    with pytest.raises(SystemExit, match="auth failed"):
        orch.cmd_apply(
            _make_args(tmp_path, cfg),
            display_name="Brave",
            running_fn=lambda: False,
            find_cmdline_fn=lambda: None,
            restart_fn=lambda _cmd: [],
            build_plans_fn=_build_pwa_only_plans,
        )


@pytest.mark.skipif(
    sys.platform == "win32", reason="Linux/macOS-only path; Windows uses IsUserAnAdmin"
)
def test_sudo_preflight_handles_calledprocesserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the interactive `sudo -v` fails (user dismisses the prompt or
    is not in sudoers), the same clean error must fire."""
    import subprocess as sp

    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")

    cfg = tmp_path / "x.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com/"]\n')

    def fake_run(cmd, *args, **kwargs):
        if list(cmd[:3]) == ["sudo", "-n", "true"]:
            return sp.CompletedProcess(cmd, 1)  # not cached
        if list(cmd[:2]) == ["sudo", "-v"]:
            raise sp.CalledProcessError(1, cmd)
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(orch.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match="auth failed"):
        orch.cmd_apply(
            _make_args(tmp_path, cfg),
            display_name="Brave",
            running_fn=lambda: False,
            find_cmdline_fn=lambda: None,
            restart_fn=lambda _cmd: [],
            build_plans_fn=_build_pwa_only_plans,
        )
