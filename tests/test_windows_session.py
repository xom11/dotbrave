"""Windows cross-session process handling.

When dotbrave runs outside the interactive desktop session (e.g. over
SSH: sshd commands land in session 0 while the browser's windows live in
session 1), window messages and GUI launches do not cross the session
boundary: `taskkill /IM` (normal close) fails with "can only be
terminated forcefully" and a relaunched browser would be invisible.
`BrowserProcess` must route both close and launch through the
Task Scheduler interactive trampoline (`schtasks /IT`) in that case.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dotbrave._base import process as proc_mod


def _bp() -> proc_mod.BrowserProcess:
    return proc_mod.BrowserProcess(
        display_name="Brave",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser",
        linux_wrappers=["brave-browser", "brave"],
        windows_exe_relpath=(
            "BraveSoftware", "Brave-Browser", "Application", "brave.exe"
        ),
    )


@pytest.fixture
def win32(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "win32")


def test_close_routes_through_trampoline_cross_session(
    win32, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: True
    )
    calls: list[str] = []
    monkeypatch.setattr(
        proc_mod, "_run_in_console_session",
        lambda cmd: calls.append(cmd) or True,
    )
    # Plain taskkill must NOT be attempted cross-session.
    monkeypatch.setattr(
        proc_mod.subprocess, "run",
        lambda *a, **k: pytest.fail(f"unexpected subprocess.run: {a}"),
    )
    bp = _bp()
    monkeypatch.setattr(bp, "running", lambda: False)
    bp.close_and_wait(timeout=0.2)
    assert calls == ["taskkill /IM brave.exe"]


def test_close_uses_plain_taskkill_same_session(
    win32, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: False
    )
    monkeypatch.setattr(
        proc_mod, "_run_in_console_session",
        lambda cmd: pytest.fail("trampoline must not be used in-session"),
    )
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        proc_mod.subprocess, "run",
        lambda *a, **k: run_calls.append(list(a[0]))
        or subprocess.CompletedProcess(a, 0),
    )
    bp = _bp()
    monkeypatch.setattr(bp, "running", lambda: False)
    bp.close_and_wait(timeout=0.2)
    assert ["taskkill", "/IM", "brave.exe"] in run_calls


def test_close_cross_session_without_trampoline_errors(
    win32, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: True
    )
    monkeypatch.setattr(
        proc_mod, "_run_in_console_session", lambda cmd: False
    )
    bp = _bp()
    monkeypatch.setattr(bp, "running", lambda: True)
    with pytest.raises(SystemExit) as exc:
        bp.close_and_wait(timeout=0.2)
    assert "desktop session" in str(exc.value)


def test_launch_live_routes_through_trampoline_cross_session(
    win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: True
    )
    calls: list[str] = []
    monkeypatch.setattr(
        proc_mod, "_run_in_console_session",
        lambda cmd: calls.append(cmd) or True,
    )
    monkeypatch.setattr(
        proc_mod.subprocess, "Popen",
        lambda *a, **k: pytest.fail("Popen must not run cross-session"),
    )
    bp = _bp()
    cmdline = bp.launch_live(tmp_path / "root", "Default", 9333)
    assert len(calls) == 1
    assert "--remote-debugging-port=9333" in calls[0]
    assert str(exe) in calls[0]
    assert "--remote-debugging-port=9333" in " ".join(cmdline)


def test_restart_routes_through_trampoline_cross_session(
    win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: True
    )
    calls: list[str] = []
    monkeypatch.setattr(
        proc_mod, "_run_in_console_session",
        lambda cmd: calls.append(cmd) or True,
    )
    monkeypatch.setattr(
        proc_mod.subprocess, "Popen",
        lambda *a, **k: pytest.fail("Popen must not run cross-session"),
    )
    bp = _bp()
    bp.restart([str(exe)])
    assert calls and str(exe) in calls[0]


def test_launch_live_popen_same_session(
    win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        proc_mod, "_windows_console_session_mismatch", lambda: False
    )
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        proc_mod.subprocess, "Popen",
        lambda cmdline, **k: popen_calls.append(list(cmdline)),
    )
    bp = _bp()
    bp.launch_live(tmp_path / "root", "Default", 9333)
    assert popen_calls and str(exe) == popen_calls[0][0]


def test_session_mismatch_is_false_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    assert proc_mod._windows_console_session_mismatch() is False
