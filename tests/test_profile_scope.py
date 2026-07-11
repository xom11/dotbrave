"""Process detection/close must be scoped to the target --user-data-dir.

Regression tests for the Linux bug where a stable-channel apply against
one profile root closed *every* running Brave (`pgrep`/`pkill -x brave`
are global).  On Linux an explicitly-launched Brave keeps
``--user-data-dir=<root>`` on its main process and all children, while a
default-launched Brave (opened from the app menu) carries the flag
nowhere -- so a pid's own cmdline is enough to tell which instance/root
it belongs to.
"""
from __future__ import annotations

import importlib

import pytest


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
