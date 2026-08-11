"""Tests for platform-aware logic (profile root, process name).

We avoid actually invoking subprocess on a real Brave; we just verify
that the module-level dispatch picks the right code path per
`sys.platform`.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "platform,expected_suffix",
    [
        ("darwin", Path("Library") / "Application Support" / "BraveSoftware" / "Brave-Browser"),
        ("linux", Path(".config") / "BraveSoftware" / "Brave-Browser"),
        ("linux2", Path(".config") / "BraveSoftware" / "Brave-Browser"),
    ],
)
def test_default_profile_root_supported(monkeypatch, platform, expected_suffix) -> None:
    monkeypatch.setattr("sys.platform", platform)
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    root = brave_pkg._default_profile_root()
    assert root is not None
    assert root.is_absolute()
    # The trailing path components should match what we expect for this OS
    assert str(root).endswith(str(expected_suffix))


def test_default_profile_root_unsupported_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd13")
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() is None


def test_default_profile_root_windows(monkeypatch, tmp_path) -> None:
    """Windows Brave profile lives under %LOCALAPPDATA%."""
    monkeypatch.setattr("sys.platform", "win32")
    fake_local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    win_root = fake_local / "BraveSoftware" / "Brave-Browser" / "User Data"
    _make_brave_profile(win_root)

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    root = brave_pkg._default_profile_root()
    assert root is not None
    assert root == win_root


def test_default_profile_root_windows_no_localappdata(monkeypatch) -> None:
    """If LOCALAPPDATA is not set, return None (require --profile-root)."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() is None


def _make_brave_profile(root: Path) -> None:
    """Materialize the minimum a `Local State` probe will accept."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text("{}")


def test_default_profile_root_linux_picks_snap_when_only_snap_has_data(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    snap_root = tmp_path / "snap" / "brave" / "current" / ".config" / "BraveSoftware" / "Brave-Browser"
    _make_brave_profile(snap_root)

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() == snap_root


def test_default_profile_root_linux_prefers_direct_install_over_snap(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    deb_root = tmp_path / ".config" / "BraveSoftware" / "Brave-Browser"
    snap_root = tmp_path / "snap" / "brave" / "current" / ".config" / "BraveSoftware" / "Brave-Browser"
    _make_brave_profile(deb_root)
    _make_brave_profile(snap_root)

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() == deb_root


def test_default_profile_root_linux_falls_back_to_direct_when_neither_populated(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    deb_root = tmp_path / ".config" / "BraveSoftware" / "Brave-Browser"

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() == deb_root


def test_default_profile_root_linux_picks_flatpak_when_only_flatpak_has_data(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    flatpak_root = (
        tmp_path / ".var" / "app" / "com.brave.Browser" / "config"
        / "BraveSoftware" / "Brave-Browser"
    )
    _make_brave_profile(flatpak_root)

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    assert brave_pkg._default_profile_root() == flatpak_root


# ---------------------------------------------------------------------------
# Channel-aware path resolution (Beta / Nightly)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "channel,suffix",
    [("stable", ""), ("beta", "-Beta"), ("nightly", "-Nightly")],
)
def test_default_profile_root_macos_per_channel(
    monkeypatch, tmp_path, channel, suffix
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)

    root = brave_pkg._default_profile_root(channel)
    assert root == (
        tmp_path / "Library" / "Application Support" / "BraveSoftware"
        / f"Brave-Browser{suffix}"
    )


@pytest.mark.parametrize(
    "channel,suffix",
    [("stable", ""), ("beta", "-Beta"), ("nightly", "-Nightly")],
)
def test_default_profile_root_linux_per_channel(
    monkeypatch, tmp_path, channel, suffix
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)

    root = brave_pkg._default_profile_root(channel)
    assert root == (
        tmp_path / ".config" / "BraveSoftware" / f"Brave-Browser{suffix}"
    )


def test_linux_beta_skips_snap_flatpak_probe(monkeypatch, tmp_path) -> None:
    """Snap/Flatpak only ship stable -- beta must return the direct path
    even when a stable snap profile happens to exist."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    snap_root = (
        tmp_path / "snap" / "brave" / "current" / ".config"
        / "BraveSoftware" / "Brave-Browser"
    )
    _make_brave_profile(snap_root)  # stable snap exists

    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)
    beta = brave_pkg._default_profile_root("beta")
    assert beta == tmp_path / ".config" / "BraveSoftware" / "Brave-Browser-Beta"


@pytest.mark.parametrize(
    "channel,suffix",
    [("stable", ""), ("beta", "-Beta"), ("nightly", "-Nightly")],
)
def test_default_profile_root_windows_per_channel(
    monkeypatch, tmp_path, channel, suffix
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    fake_local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    import dotbrave.browser as brave_pkg
    importlib.reload(brave_pkg)

    root = brave_pkg._default_profile_root(channel)
    assert root == fake_local / "BraveSoftware" / f"Brave-Browser{suffix}" / "User Data"


def test_default_profile_root_rejects_unknown_channel() -> None:
    import dotbrave.browser as brave_pkg
    with pytest.raises(ValueError, match="unknown channel"):
        brave_pkg._default_profile_root("dev")


def test_make_browser_process_channel_distinct_macos_app_names() -> None:
    """macOS proc name + app name change per channel so kill/restart
    target the right Brave install."""
    from dotbrave.utils import _make_browser_process
    assert _make_browser_process("stable").macos_app_name == "Brave Browser"
    assert _make_browser_process("beta").macos_app_name == "Brave Browser Beta"
    assert _make_browser_process("nightly").macos_app_name == "Brave Browser Nightly"
    # Display names too
    assert _make_browser_process("beta").display_name == "Brave Beta"


def test_make_browser_process_windows_path_per_channel() -> None:
    from dotbrave.utils import _make_browser_process
    assert _make_browser_process("stable").windows_exe_relpath == (
        "BraveSoftware", "Brave-Browser", "Application", "brave.exe",
    )
    assert _make_browser_process("nightly").windows_exe_relpath == (
        "BraveSoftware", "Brave-Browser-Nightly", "Application", "brave.exe",
    )


def test_make_browser_process_linux_wrappers_per_channel() -> None:
    from dotbrave.utils import _make_browser_process
    assert _make_browser_process("stable").linux_wrappers == [
        "brave-browser", "brave",
    ]
    assert _make_browser_process("beta").linux_wrappers == [
        "brave-browser-beta", "brave-beta",
    ]


def test_make_browser_process_no_flatpak_for_non_stable() -> None:
    """Brave doesn't ship Beta/Nightly via Flatpak; restart should not
    try `flatpak run` for those channels."""
    from dotbrave.utils import _make_browser_process
    assert _make_browser_process("stable").flatpak_app_id == "com.brave.Browser"
    assert _make_browser_process("beta").flatpak_app_id is None
    assert _make_browser_process("nightly").flatpak_app_id is None


def test_linux_pid_filter_set_for_non_stable_only() -> None:
    """Stable keeps the permissive `pgrep`-only behavior so Snap/Flatpak
    installs aren't falsely excluded.  Beta/Nightly use a path filter
    so a beta apply doesn't kill stable (and vice versa)."""
    from dotbrave.utils import _make_browser_process
    assert _make_browser_process("stable").linux_pid_filter is None
    assert _make_browser_process("beta").linux_pid_filter == (
        "/opt/brave.com/brave-beta/"
    )
    assert _make_browser_process("nightly").linux_pid_filter == (
        "/opt/brave.com/brave-nightly/"
    )


def test_pids_filters_by_linux_pid_filter(monkeypatch) -> None:
    """With `linux_pid_filter` set on Linux, `pids()` drops pids whose
    argv[0] doesn't contain the filter substring.  This is the
    load-bearing piece that prevents a beta `pkill` from hitting stable.
    """
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    # pgrep returns three pids — two beta, one stable
    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: b"100\n200\n300\n",
    )

    cmdlines = {
        "100": ["/opt/brave.com/brave-beta/brave", "--type=renderer"],
        "200": ["/opt/brave.com/brave-beta/brave"],
        "300": ["/opt/brave.com/brave/brave"],  # stable
    }
    monkeypatch.setattr(bp, "_read_cmdline", lambda pid: cmdlines.get(pid))

    proc = bp.BrowserProcess(
        display_name="Brave Beta",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser Beta",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser Beta",
        linux_wrappers=["brave-browser-beta"],
        windows_exe_relpath=("BraveSoftware", "Brave-Browser-Beta", "Application", "brave.exe"),
        linux_pid_filter="/opt/brave.com/brave-beta/",
    )
    assert proc.pids() == ["100", "200"]
    assert proc.running() is True


def test_pids_filter_returns_empty_when_only_other_channel_running(monkeypatch) -> None:
    """If only stable is running, beta's `running()` must return False
    (the existing `pgrep` answer would have been True)."""
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: b"500\n",
    )
    monkeypatch.setattr(
        bp, "_read_cmdline",
        lambda pid: ["/opt/brave.com/brave/brave"],  # stable only
    )

    proc = bp.BrowserProcess(
        display_name="Brave Nightly",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser Nightly",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser Nightly",
        linux_wrappers=["brave-browser-nightly"],
        windows_exe_relpath=("BraveSoftware", "Brave-Browser-Nightly", "Application", "brave.exe"),
        linux_pid_filter="/opt/brave.com/brave-nightly/",
    )
    assert proc.pids() == []
    assert proc.running() is False


def test_kill_and_wait_uses_scoped_kill_when_filter_set(monkeypatch) -> None:
    """When `linux_pid_filter` is set, `kill_and_wait` must NOT issue
    `pkill -x brave` (which would kill every channel).  It should
    `kill -KILL <pid>...` against only the filtered set.
    """
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: b"42\n",
    )
    monkeypatch.setattr(
        bp, "_read_cmdline",
        lambda pid: ["/opt/brave.com/brave-beta/brave"],
    )

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.setdefault("calls", []).append(list(cmd))
        return bp.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bp.subprocess, "run", fake_run)

    proc = bp.BrowserProcess(
        display_name="Brave Beta",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser Beta",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser Beta",
        linux_wrappers=["brave-browser-beta"],
        windows_exe_relpath=("BraveSoftware", "Brave-Browser-Beta", "Application", "brave.exe"),
        linux_pid_filter="/opt/brave.com/brave-beta/",
    )
    # After the kill, pretend the process is gone so the wait loop exits.
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait()

    # Exactly one kill call, scoped to the filtered pid -- no pkill.
    assert captured["calls"] == [["kill", "-KILL", "42"]]


def test_kill_and_wait_keeps_pkill_when_no_filter(monkeypatch) -> None:
    """Stable Brave keeps the `pkill -x brave` path so Snap/Flatpak
    installs (which the filter would exclude) still get killed."""
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.setdefault("calls", []).append(list(cmd))
        return bp.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bp.subprocess, "run", fake_run)

    proc = bp.BrowserProcess(
        display_name="Brave",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser",
        linux_wrappers=["brave-browser"],
        windows_exe_relpath=("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        linux_pid_filter=None,
    )
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait()

    assert captured["calls"] == [["pkill", "-KILL", "-x", "brave"]]


def test_restart_uses_flatpak_run_for_flatpak_brave(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr(bp.subprocess, "Popen", FakePopen)
    used = utils.restart_brave([
        "/app/brave/brave",
        "--disable-features=Foo",
        "--no-first-run",
    ])
    assert used[0] == "flatpak"
    assert used[1] == "run"
    assert used[2] == "com.brave.Browser"
    assert "--disable-features=Foo" in used
    assert captured["cmd"] == used


@pytest.mark.skipif(sys.platform == "win32", reason="Snap does not exist on Windows")
def test_pwa_refuses_snap_install(tmp_path) -> None:
    from dotbrave import pwa
    snap_prefs = (
        tmp_path / "snap" / "brave" / "current" / ".config" / "BraveSoftware"
        / "Brave-Browser" / "Default" / "Preferences"
    )
    with pytest.raises(SystemExit) as exc:
        pwa.plan_apply(snap_prefs, {}, {"urls": ["https://squoosh.app/"]})
    assert "Snap" in str(exc.value)


@pytest.mark.skipif(sys.platform == "win32", reason="Flatpak does not exist on Windows")
def test_pwa_refuses_flatpak_install(tmp_path) -> None:
    from dotbrave import pwa
    flatpak_prefs = (
        tmp_path / ".var" / "app" / "com.brave.Browser" / "config"
        / "BraveSoftware" / "Brave-Browser" / "Default" / "Preferences"
    )
    with pytest.raises(SystemExit) as exc:
        pwa.plan_apply(flatpak_prefs, {}, {"urls": ["https://squoosh.app/"]})
    assert "Flatpak" in str(exc.value)


def test_pwa_accepts_direct_install(tmp_path, monkeypatch) -> None:
    from dotbrave import pwa
    monkeypatch.setattr(pwa, "_read_current_policy", lambda: [])
    deb_prefs = (
        Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"
        / "Default" / "Preferences"
    )
    plan = pwa.plan_apply(deb_prefs, {}, {"urls": ["https://squoosh.app/"]})
    assert plan.namespace == "pwa"


@pytest.mark.parametrize(
    "platform,expected_name",
    [("darwin", "Brave Browser"), ("linux", "brave"), ("linux2", "brave"), ("win32", "brave.exe")],
)
def test_proc_name_per_platform(monkeypatch, platform, expected_name) -> None:
    monkeypatch.setattr("sys.platform", platform)
    from dotbrave import utils
    importlib.reload(utils)
    assert utils._brave_proc_name() == expected_name


def test_restart_uses_open_on_macos(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(bp.subprocess, "Popen", FakePopen)
    used = utils.restart_brave([
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "--enable-features=Foo",
    ])
    assert used[0] == "open"
    assert used[1] == "-a"
    assert used[2] == "Brave Browser"
    assert "--args" in used
    assert "--enable-features=Foo" in used
    assert captured["cmd"] == used
    assert captured["kwargs"].get("start_new_session") is True


def test_read_cmdline_macos_does_not_shlex_split(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    raw = b"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser\n"

    def fake_check_output(cmd, **kwargs):
        assert cmd[0] == "ps"
        return raw

    monkeypatch.setattr(bp.subprocess, "check_output", fake_check_output)
    out = bp._read_cmdline("12345")
    assert out == ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]


def test_restart_uses_wrapper_on_linux(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    monkeypatch.setattr(bp.shutil, "which", lambda name: "/usr/bin/brave-browser" if name == "brave-browser" else None)

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr(bp.subprocess, "Popen", FakePopen)
    used = utils.restart_brave(["/opt/brave.com/brave/brave", "--flag=1"])
    assert used[0] == "/usr/bin/brave-browser"
    assert "--flag=1" in used


# --- Windows-specific tests ---


def test_brave_running_windows(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    csv_output = b'"brave.exe","14796","Console","1","288,820 K"\r\n'

    def fake_check_output(cmd, **kwargs):
        assert "tasklist" in cmd
        return csv_output

    monkeypatch.setattr(bp.subprocess, "check_output", fake_check_output)
    assert utils.brave_running() is True


def test_brave_running_windows_not_running(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    def fake_check_output(cmd, **kwargs):
        raise bp.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(bp.subprocess, "check_output", fake_check_output)
    assert utils.brave_running() is False


def test_brave_pids_windows(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    csv_output = (
        b'"brave.exe","14796","Console","1","288,820 K"\r\n'
        b'"brave.exe","18928","Console","1","178,464 K"\r\n'
    )

    def fake_check_output(cmd, **kwargs):
        return csv_output

    monkeypatch.setattr(bp.subprocess, "check_output", fake_check_output)
    pids = utils._brave_pids()
    assert pids == ["14796", "18928"]


def test_read_cmdline_windows(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    importlib.reload(bp)

    raw = b'"C:\\Users\\test\\AppData\\Local\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"\r\n'

    def fake_check_output(cmd, **kwargs):
        assert "powershell" in cmd
        return raw

    monkeypatch.setattr(bp.subprocess, "check_output", fake_check_output)
    out = bp._read_cmdline("14796")
    assert out is not None
    assert len(out) == 1
    assert "brave.exe" in out[0]


def test_kill_uses_taskkill_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return bp.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    # Patch running() on the BrowserProcess to return False so the wait loop exits
    monkeypatch.setattr(utils.BROWSER_PROCESS, "running", lambda: False)
    utils.kill_brave_and_wait()
    assert captured["cmd"][0] == "taskkill"
    assert "/F" in captured["cmd"]
    assert "/IM" in captured["cmd"]
    assert "brave.exe" in captured["cmd"]


def test_restart_windows_uses_known_exe(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from dotbrave._base import process as bp
    from dotbrave import utils
    importlib.reload(bp)
    importlib.reload(utils)

    fake_local = tmp_path / "AppData" / "Local"
    brave_dir = fake_local / "BraveSoftware" / "Brave-Browser" / "Application"
    brave_dir.mkdir(parents=True)
    brave_exe = brave_dir / "brave.exe"
    brave_exe.write_text("fake")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr(bp.subprocess, "Popen", FakePopen)
    # This test covers exe selection + the plain Popen path; on a real
    # Windows host reached over SSH the cross-session trampoline would
    # kick in, so pin it off.
    monkeypatch.setattr(bp, "_windows_console_session_mismatch", lambda: False)
    used = utils.restart_brave(["captured-cmdline"])
    assert str(brave_exe) in used[0]
    assert captured["cmd"] == used
