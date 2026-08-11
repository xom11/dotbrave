"""Process detection/close must be scoped to the target --user-data-dir.

Regression tests for the Linux bug where a stable-channel apply against
one profile root closed *every* running Brave (`pgrep`/`pkill -x brave`
are global).  On Linux an explicitly-launched Brave keeps
``--user-data-dir=<root>`` on its main process and all children, while a
default-launched Brave (opened from the app menu) carries the flag
nowhere -- so a pid's own cmdline is enough to tell which instance/root
it belongs to.

Also covers a finer-grained scope: a running browser can hold the
*root* open without holding the specific *profile* dotbrave is asked to
apply to.  Chromium only keeps a profile's Preferences in memory while
that profile is loaded, so a profile that is not open can be written
offline right now, with no close at all.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

import pytest

from dotbrave._base import orchestrator as orch
from dotbrave._base.utils import Plan


def _linux_process_module(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)
    return bp


def _make_proc(bp, *, linux_pid_filter=None):
    return bp.BrowserProcess(
        display_name="Brave",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser",
        linux_wrappers=["brave-browser"],
        windows_exe_relpath=(
            "BraveSoftware", "Brave-Browser", "Application", "brave.exe",
        ),
        linux_pid_filter=linux_pid_filter,
    )


DEFAULT_ROOT = "/home/u/.config/BraveSoftware/Brave-Browser"
OTHER_ROOT = "/tmp/udd"


def _wire_pgrep(monkeypatch, bp, cmdlines):
    pids = "".join(f"{p}\n" for p in cmdlines)
    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: pids.encode()
    )
    monkeypatch.setattr(bp, "_read_cmdline", lambda pid: cmdlines.get(pid))


def test_scope_excludes_other_profile_root(monkeypatch) -> None:
    """The exact bug: a default-launched Brave (flagless) must NOT be
    seen as running when we target a *different* explicit root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        # default-launched Brave the user is actively using
        "100": ["/opt/brave.com/brave/brave"],
        "101": ["/opt/brave.com/brave/brave", "--type=renderer"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == []
    assert proc.running() is False


def test_scope_matches_explicit_target(monkeypatch) -> None:
    """Only the instance whose cmdline carries --user-data-dir=<target>
    is selected; a co-running default instance is left out."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # default, untouched
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}",
                "--profile-directory=Default"],  # target main
        "201": ["/opt/brave.com/brave/brave", "--type=zygote",
                f"--user-data-dir={OTHER_ROOT}"],  # target child
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == ["200", "201"]
    assert proc.running() is True


def test_scope_default_target_includes_flagless_and_matching(monkeypatch) -> None:
    """Targeting the default root selects both flagless (menu-launched)
    Brave and any instance explicitly pointed at the default root, but
    excludes a second explicit root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # flagless default
        "110": ["/opt/brave.com/brave/brave", f"--user-data-dir={DEFAULT_ROOT}"],
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(DEFAULT_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == ["100", "110"]


def test_close_and_wait_scoped_kill_not_global_pkill(monkeypatch) -> None:
    """With a profile scope active on stable (no channel filter),
    close_and_wait must SIGTERM only the scoped pids -- never
    `pkill -TERM -x brave`, which would hit the user's other Brave."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # default -- must survive
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.close_and_wait(timeout=0.2)

    assert calls == [["kill", "-TERM", "200"]]


def test_kill_and_wait_scoped_kill_not_global_pkill(monkeypatch) -> None:
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait(timeout=0.2)

    assert calls == [["kill", "-KILL", "200"]]


def test_unscoped_behavior_unchanged(monkeypatch) -> None:
    """Without a scope set, stable keeps the permissive global pgrep/pkill
    behavior (Snap/Flatpak installs rely on it)."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],
        "200": ["/snap/brave/x/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)  # no scope_to_profile
    assert proc.pids() == ["100", "200"]
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait(timeout=0.2)
    assert calls == [["pkill", "-KILL", "-x", "brave"]]


def test_scope_combines_with_channel_filter(monkeypatch) -> None:
    """Beta channel + profile scope: a pid must match BOTH the channel
    path filter and the target root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        # beta at target root -- keep
        "100": ["/opt/brave.com/brave-beta/brave", f"--user-data-dir={OTHER_ROOT}"],
        # beta at a different root -- drop
        "200": ["/opt/brave.com/brave-beta/brave", "--user-data-dir=/tmp/z"],
        # stable at target root -- drop (wrong channel)
        "300": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp, linux_pid_filter="/opt/brave.com/brave-beta/")
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    assert proc.pids() == ["100"]


# ---------------------------------------------------------------------------
# `cmd_apply(profile_open_fn=...)`: skip the close for a profile that is
# not open in a browser that IS running on the target --user-data-dir.
# ---------------------------------------------------------------------------

def _args(profile_root: Path, config: Path, **overrides) -> argparse.Namespace:
    values = {
        "profile_root": profile_root,
        "profile": "Default",
        "config": str(config),
        "dry_run": False,
        "allow_http": False,
        "expect_sha256": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"foo": {"bar": 0}}))
    return tmp_path


def _build_plan(prefs_path: Path, _prefs: dict, _doc: dict, **_kw) -> list[Plan]:
    def apply_fn(prefs: dict) -> None:
        prefs["foo"]["bar"] = 1

    return [
        Plan(
            namespace="settings",
            diff_lines=["  ~ foo.bar: 0 -> 1"],
            apply_fn=apply_fn,
            verify_fn=lambda _prefs: None,
            state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
            state_payload={"managed_keys": ["foo.bar"]},
        )
    ]


def test_apply_to_a_profile_that_is_not_open_never_closes_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    closed: list[str] = []

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,          # a Brave IS running...
        profile_open_fn=lambda: False,    # ...but not on this profile
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=lambda *a, **k: pytest.fail("live apply not needed"),
        graceful_close_fn=lambda: closed.append("closed"),
        launch_live_fn=lambda *a, **k: ["brave"],
    )

    assert closed == [], "closed a browser that does not hold this profile"
    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    assert prefs["foo"]["bar"] == 1


def test_apply_when_profile_open_fn_reports_open_still_closes(
    tmp_path: Path,
) -> None:
    """The conservative default: when `profile_open_fn` reports the
    profile IS open, behavior is unchanged from a plain running browser
    with no live adapter -- close normally and restart."""
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    calls: list[tuple[str, object]] = []

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        profile_open_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda cmd: calls.append(("restart", cmd)) or cmd,
        build_plans_fn=_build_plan,
        graceful_close_fn=lambda: calls.append(("close", None)),
    )

    assert calls == [("close", None), ("restart", ["brave"])]


def test_apply_without_profile_open_fn_keeps_todays_behavior(
    tmp_path: Path,
) -> None:
    """`profile_open_fn` defaults to None -- every existing caller that
    doesn't pass it keeps closing a running browser exactly as before."""
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    calls: list[tuple[str, object]] = []

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda cmd: calls.append(("restart", cmd)) or cmd,
        build_plans_fn=_build_plan,
        graceful_close_fn=lambda: calls.append(("close", None)),
    )

    assert calls == [("close", None), ("restart", ["brave"])]


def test_unattended_apply_to_a_profile_that_is_not_open_applies_fully(
    tmp_path: Path,
) -> None:
    """`--unattended` normally reports-and-skips a running browser rather
    than close it (exit 0, nothing written).  When the browser doesn't
    hold this profile, there's nothing for that early-exit to protect --
    the run should go all the way through, not skip."""
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")

    orch.cmd_apply(
        _args(profile_root, cfg, unattended=True),
        display_name="Brave",
        running_fn=lambda: True,
        profile_open_fn=lambda: False,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=lambda *a, **k: pytest.fail("live apply not needed"),
        graceful_close_fn=lambda: pytest.fail("must not close the browser"),
        launch_live_fn=lambda *a, **k: ["brave"],
    )

    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    assert prefs["foo"]["bar"] == 1
