"""OSCrypt / Keychain / keyring session safety across browser launch paths.

Regression coverage for the 19/11/2026 data-loss incident on macOS 26.6 /
Brave 158: ``dotbrave apply`` run over SSH skipped a WS-only
``brave://inspect`` endpoint, closed the running GUI Brave, and spawned
``Brave Browser --headless=new`` inside the SSH ``Background`` launchd
session. Without Aqua / login-Keychain access to ``"Brave Safe Storage"``
(``errSecInteractionNotAllowed``), Chromium failed to decrypt every ``v10``
cookie and permanently wiped ``Default/Cookies``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave import extensions
from dotbrave._base import orchestrator
from dotbrave._base import process as proc_mod
from dotbrave.utils import _make_browser_process


def _make_ext(root: Path, name: str = "tabjump") -> Path:
    ext_dir = root / name
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": name,
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "bg.js").write_text("console.log('ok');\n", encoding="utf-8")
    return ext_dir


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    root = tmp_path / "profile_root"
    prof = root / "Default"
    prof.mkdir(parents=True)
    (prof / "Preferences").write_text(
        json.dumps({"brave": {"tabs": {"vertical_tabs_enabled": False}}}),
        encoding="utf-8",
    )
    return root


def _apply_args(
    profile_root: Path, cfg: Path, *, unattended: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(cfg),
        dry_run=False,
        unattended=unattended,
        channel="stable",
        skip=[],
    )


# ---------------------------------------------------------------------------
# 1. macOS _macos_keychain_session_blocker matrix
# ---------------------------------------------------------------------------


def test_macos_blocker_allows_unlocked_aqua_even_with_ssh_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LaunchAgents inside Aqua (e.g. ``orca-serve``) may set ``SSH_TTY=1`` for
    OSC52 clipboard integration while holding full Aqua + Keychain access."""
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("SSH_TTY", "1")
    monkeypatch.setattr(proc_mod, "_macos_launchctl_managername", lambda: "Aqua")
    monkeypatch.setattr(
        proc_mod, "_macos_security_session_attrs", lambda: 0x2030
    )
    monkeypatch.setattr(
        proc_mod, "_macos_default_keychain_unlocked", lambda: True
    )

    assert proc_mod._macos_keychain_session_blocker("Brave") is None


def test_macos_blocker_refuses_ssh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 55555 198.51.100.2 22")

    reason = proc_mod._macos_keychain_session_blocker("Brave")
    assert reason is not None
    assert "SSH" in reason and "Keychain" in reason and "Cookies" in reason


def test_macos_blocker_refuses_non_aqua_launchd_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )

    reason = proc_mod._macos_keychain_session_blocker("Brave")
    assert reason is not None
    assert "'Background'" in reason and "'Aqua'" in reason


@pytest.mark.parametrize("attrs", [0x0020, 0x5020, 0x1010])
def test_macos_blocker_refuses_non_graphic_or_remote_security_session(
    monkeypatch: pytest.MonkeyPatch, attrs: int
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(proc_mod, "_macos_launchctl_managername", lambda: "Aqua")
    monkeypatch.setattr(proc_mod, "_macos_security_session_attrs", lambda: attrs)

    reason = proc_mod._macos_keychain_session_blocker("Brave")
    assert reason is not None
    assert "SessionGetInfo" in reason


def test_macos_blocker_fails_closed_when_session_cannot_be_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(proc_mod, "_macos_launchctl_managername", lambda: None)
    monkeypatch.setattr(proc_mod, "_macos_security_session_attrs", lambda: None)

    reason = proc_mod._macos_keychain_session_blocker("Brave")
    assert reason is not None
    assert "cannot verify" in reason


@pytest.mark.parametrize("kc_state", [False, None])
def test_macos_blocker_refuses_locked_or_unreadable_login_keychain(
    monkeypatch: pytest.MonkeyPatch, kc_state: bool | None
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(proc_mod, "_macos_launchctl_managername", lambda: "Aqua")
    monkeypatch.setattr(
        proc_mod, "_macos_security_session_attrs", lambda: 0x2030
    )
    monkeypatch.setattr(
        proc_mod, "_macos_default_keychain_unlocked", lambda: kc_state
    )

    reason = proc_mod._macos_keychain_session_blocker("Brave")
    assert reason is not None
    assert "security unlock-keychain" in reason


# ---------------------------------------------------------------------------
# 2. Running Brave with WS-only port + [extensions] over SSH on macOS
# ---------------------------------------------------------------------------


def test_macos_ssh_refuses_ws_only_extensions_apply_before_closing_or_backup(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brave running with a WS-only ``brave://inspect`` port while
    ``dotbrave apply`` runs over SSH with an ``[extensions] unpacked`` change.
    Must fail closed BEFORE closing Brave, taking a backup, or spawning
    headless Brave."""
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    prefs_path = profile_root / "Default" / "Preferences"
    before_bytes = prefs_path.read_bytes()

    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 54459 198.51.100.2 22")
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(
        proc_mod, "_macos_security_session_attrs", lambda: 0x5020
    )
    monkeypatch.setattr(
        proc_mod, "_macos_default_keychain_unlocked", lambda: False
    )

    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(
        brave_pkg,
        "find_main_brave_cmdline",
        lambda: ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    )
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(
        orchestrator, "devtools_port_is_ws_only", lambda r, port: True
    )

    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda *a, **k: pytest.fail("must NOT close running Brave over SSH"),
    )
    monkeypatch.setattr(
        extensions,
        "apply_headless",
        lambda *a, **k: pytest.fail("must NOT run apply_headless over SSH"),
    )
    monkeypatch.setattr(
        proc_mod.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT spawn Brave over SSH"),
    )

    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))

    msg = str(exc.value)
    assert "[extensions] not applied" in msg
    assert "was left running" in msg
    assert "Keychain" in msg
    assert prefs_path.read_bytes() == before_bytes
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []
    assert not extensions.state_file(prefs_path).exists()


# ---------------------------------------------------------------------------
# 3. Closed Brave over SSH / locked Keychain on macOS: [extensions] vs [settings]
# ---------------------------------------------------------------------------


def test_macos_closed_brave_refuses_headless_extensions_before_backup(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Brave is already closed on macOS over SSH (or with a locked
    Keychain), ``[extensions]`` headless apply must refuse BEFORE taking a
    Preferences backup or spawning ``--headless=new``."""
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[settings]\n"brave.tabs.vertical_tabs_enabled" = true\n'
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    prefs_path = profile_root / "Default" / "Preferences"
    before_bytes = prefs_path.read_bytes()

    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    monkeypatch.setattr(
        proc_mod.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT spawn headless Brave"),
    )
    monkeypatch.setattr(
        extensions.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT spawn headless Brave"),
    )

    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))

    msg = str(exc.value)
    assert "[settings], [extensions] not applied" in msg
    assert "headless Brave launch" in msg
    assert "Keychain" in msg
    assert prefs_path.read_bytes() == before_bytes
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []
    assert not extensions.state_file(prefs_path).exists()


def test_macos_closed_brave_unattended_skips_headless_extensions_and_exits_zero(
    profile_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under ``--unattended`` with Brave closed and Keychain locked/unavailable:
    1) ``[extensions]`` alone warns on stderr, takes NO backup, and exits 0.
    2) Mixed ``[settings]`` + ``[extensions]`` skips ``[extensions]`` (no
       headless launch) while still applying plain JSON ``[settings]``."""
    ext_dir = _make_ext(tmp_path, "tabjump")
    prefs_path = profile_root / "Default" / "Preferences"

    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(
        proc_mod, "_macos_default_keychain_unlocked", lambda: False
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    monkeypatch.setattr(
        extensions.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT spawn headless Brave"),
    )

    # 1) [extensions] only -> exit 0, zero backups, Preferences untouched.
    only_ext_cfg = tmp_path / "ext_only.toml"
    only_ext_cfg.write_text(
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    before_bytes = prefs_path.read_bytes()
    brave_pkg.cmd_apply(_apply_args(profile_root, only_ext_cfg, unattended=True))
    err = capsys.readouterr().err
    assert "unattended: skipping [extensions]" in err
    assert "Keychain" in err
    assert prefs_path.read_bytes() == before_bytes
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []

    # 2) [settings] + [extensions] -> [settings] applies, [extensions] skipped.
    mixed_cfg = tmp_path / "mixed.toml"
    mixed_cfg.write_text(
        '[settings]\n"brave.tabs.vertical_tabs_enabled" = true\n'
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    brave_pkg.cmd_apply(_apply_args(profile_root, mixed_cfg, unattended=True))
    captured = capsys.readouterr()
    assert "unattended: skipping [extensions]" in captured.err
    assert "ok -- applied and verified" in captured.out
    reloaded = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert reloaded["brave"]["tabs"]["vertical_tabs_enabled"] is True
    assert not extensions.state_file(prefs_path).exists()


def test_macos_closed_brave_over_ssh_still_applies_plain_json_settings(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain offline JSON edits (``[settings]``, ``[shortcuts]``) while Brave
    is already closed never launch Brave, so they remain safe over SSH."""
    cfg = tmp_path / "settings.toml"
    cfg.write_text(
        '[settings]\n"brave.tabs.vertical_tabs_enabled" = true\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 1234 198.51.100.2 22")
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))
    reloaded = json.loads(
        (profile_root / "Default" / "Preferences").read_text(encoding="utf-8")
    )
    assert reloaded["brave"]["tabs"]["vertical_tabs_enabled"] is True


# ---------------------------------------------------------------------------
# 4. macOS direct binary bootstrap / relaunch & defense-in-depth guards
# ---------------------------------------------------------------------------


def test_macos_refuses_live_bootstrap_relaunch_when_keychain_unavailable(
    profile_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running Brave with NO endpoint: ``dotbrave apply`` would close Brave and
    exec the bundle binary directly via ``launch_live``. Must refuse in both
    interactive (error exit) and ``--unattended`` (skip exit 0) modes without
    closing or launching Brave."""
    cfg = tmp_path / "settings.toml"
    cfg.write_text(
        '[settings]\n"brave.tabs.vertical_tabs_enabled" = true\n',
        encoding="utf-8",
    )
    prefs_path = profile_root / "Default" / "Preferences"
    before_bytes = prefs_path.read_bytes()

    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(
        proc_mod, "_macos_default_keychain_unlocked", lambda: False
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(
        brave_pkg,
        "find_main_brave_cmdline",
        lambda: ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    )
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda *a, **k: pytest.fail("must NOT close Brave"),
    )
    monkeypatch.setattr(
        proc_mod.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT spawn Brave"),
    )

    # Interactive: non-zero SystemExit before closing or backing up.
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))
    assert "was left running" in str(exc.value)
    assert "Keychain" in str(exc.value)
    assert prefs_path.read_bytes() == before_bytes
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []

    # Unattended: stderr warning + exit 0 before closing or backing up.
    brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=True))
    err = capsys.readouterr().err
    assert "unattended:" in err and "[settings] not applied" in err
    assert prefs_path.read_bytes() == before_bytes
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []


def test_direct_launch_live_and_apply_headless_have_defense_in_depth(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct callers of ``BrowserProcess.launch_live`` and
    ``extensions.apply_headless`` also fail closed before ``subprocess.Popen``."""
    fake_app = tmp_path / "Brave Browser.app" / "Contents" / "MacOS"
    fake_app.mkdir(parents=True)
    fake_exe = fake_app / "Brave Browser"
    fake_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(proc_mod, "_MACOS_APP_DIRS", (tmp_path,))
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(
        proc_mod.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Popen must never run in Background session"),
    )
    monkeypatch.setattr(
        extensions.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Popen must never run in Background session"),
    )

    proc = _make_browser_process("stable")
    with pytest.raises(SystemExit, match="refusing to launch Brave"):
        proc.launch_live(profile_root, "Default", 9222)

    ext_dir = _make_ext(tmp_path, "tabjump")
    plan = extensions.plan_apply(
        profile_root / "Default" / "Preferences",
        {},
        {"unpacked": [str(ext_dir)]},
        config_dir=tmp_path,
    )
    with pytest.raises(SystemExit, match=r"cannot apply \[extensions\] offline"):
        extensions.apply_headless(profile_root, "Default", plan, proc=proc)


# ---------------------------------------------------------------------------
# 5. Linux keyring / desktop-session protection for apply_headless
# ---------------------------------------------------------------------------


def test_linux_headless_blocker_refuses_bare_ssh_and_borrows_desktop_env(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: False)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(
        proc_mod.shutil, "which", lambda name: "/usr/bin/brave-browser"
    )

    proc = _make_browser_process("stable")

    # 1) Bare shell with no display -> blocked.
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    assert proc.headless_blocker() is not None

    # 2) SSH with forwarded X11 DISPLAY but no captured local desktop -> blocked
    # (XDG_CURRENT_DESKTOP is unset over SSH, so Chromium falls back to v10).
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 1111 198.51.100.2 22")
    assert proc.headless_blocker() is not None

    # 3) Desktop session captured from a running desktop Brave -> allowed, and
    # apply_headless forwards XDG_CURRENT_DESKTOP + DBUS_SESSION_BUS_ADDRESS!
    desktop_env = {
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "GNOME",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    monkeypatch.setattr(proc, "pids", lambda: ["4242"])
    monkeypatch.setattr(
        proc_mod, "_read_cmdline", lambda pid: ["/usr/bin/brave-browser"]
    )
    monkeypatch.setattr(proc_mod, "_find_session_env", lambda pid: desktop_env)
    proc.find_main_cmdline()
    assert proc.headless_blocker() is None

    seen_envs: list[dict[str, str] | None] = []

    class _DummyChild:
        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(
        extensions.subprocess,
        "Popen",
        lambda cmd, **kw: (seen_envs.append(kw.get("env")), _DummyChild())[1],
    )
    monkeypatch.setattr(extensions, "pick_unused_port", lambda: 9444)
    monkeypatch.setattr(
        extensions, "wait_for_devtools_endpoint", lambda *a, **k: None
    )
    monkeypatch.setattr(extensions, "discover_browser_ws_path", lambda port: None)

    class _NoopCdp:
        def __init__(self, port: int, ws_path: str | None = None) -> None:
            pass

        def create_page(self, url: str) -> dict:
            return {"id": "1"}

        def close_page(self, target: dict) -> None:
            pass

        def browser_command(self, method: str, params: dict | None = None) -> dict:
            return {}

        def close(self) -> None:
            pass

    monkeypatch.setattr(extensions, "CdpClient", _NoopCdp)
    monkeypatch.setattr(extensions, "apply_via_cdp", lambda *a, **k: None)

    ext_dir = _make_ext(tmp_path, "tabjump")
    plan = extensions.plan_apply(
        profile_root / "Default" / "Preferences",
        {},
        {"unpacked": [str(ext_dir)]},
        config_dir=tmp_path,
    )
    extensions.apply_headless(profile_root, "Default", plan, proc=proc)

    assert len(seen_envs) == 1 and seen_envs[0] is not None
    assert seen_envs[0]["WAYLAND_DISPLAY"] == "wayland-0"
    assert seen_envs[0]["XDG_CURRENT_DESKTOP"] == "GNOME"
    assert (
        seen_envs[0]["DBUS_SESSION_BUS_ADDRESS"]
        == "unix:path=/run/user/1000/bus"
    )


# ---------------------------------------------------------------------------
# 6. Windows Session 0 headless_blocker & trampoline refusal
# ---------------------------------------------------------------------------


def test_windows_session_zero_refuses_headless_and_failed_trampoline(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: False)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(proc_mod, "_windows_console_session_mismatch", lambda: True)

    # 1) Running Brave with skipped WS-only port over SSH -> refuses BEFORE close.
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave.exe"])
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(
        orchestrator, "devtools_port_is_ws_only", lambda r, port: True
    )
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda *a, **k: pytest.fail("must NOT close Brave when Session 0 headless is blocked"),
    )
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))
    assert "Session 0" in str(exc.value)
    assert list((profile_root / "Default").glob("Preferences.bak.*")) == []

    # 2) _spawn_detached refuses Session 0 Popen if console trampoline fails.
    proc = _make_browser_process("stable")
    monkeypatch.setattr(
        proc, "_spawn_via_console_trampoline", lambda cmdline: False
    )
    monkeypatch.setattr(
        proc_mod.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("must NOT fall back to Session 0 Popen"),
    )
    with pytest.raises(SystemExit, match="Session 0"):
        proc._spawn_detached(["brave.exe"])


def test_macos_ssh_live_http_endpoint_works_and_refuses_unsupported_close(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Brave in Aqua already exposes an HTTP ``--remote-debugging-port``,
    an SSH caller can apply live over loopback TCP without closing or spawning
    Brave -- and if ``LiveApplyUnsupported`` is raised, ``cmd_apply`` refuses
    BEFORE closing the running browser."""
    cfg = tmp_path / "settings.toml"
    cfg.write_text(
        '[settings]\n"brave.tabs.vertical_tabs_enabled" = true\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 1111 198.51.100.2 22")
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(
        brave_pkg,
        "find_main_brave_cmdline",
        lambda: [
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "--remote-debugging-port=9222",
        ],
    )
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(
        orchestrator, "devtools_port_is_ws_only", lambda r, port: False
    )
    monkeypatch.setattr(
        orchestrator, "remember_devtools_port", lambda *a, **k: None
    )
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda *a, **k: pytest.fail("must NOT close running Brave over SSH"),
    )

    live_ports: list[int] = []
    monkeypatch.setattr(
        brave_pkg.live_mod,
        "apply_live",
        lambda port, *a, **k: live_ports.append(port),
    )
    brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))
    assert live_ports == [9222]

    def _raise_unsupported(*_a, **_kw) -> None:
        raise orchestrator.LiveApplyUnsupported("Brave", ["unknown.pref"])

    monkeypatch.setattr(brave_pkg.live_mod, "apply_live", _raise_unsupported)
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(_apply_args(profile_root, cfg, unattended=False))
    assert "was left running" in str(exc.value)
    assert "Keychain" in str(exc.value)


def test_macos_ssh_undo_refuses_closing_running_brave_and_beta_channel_guarded(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apply --undo`` refuses to close a running Brave over SSH on macOS, and
    non-stable channels (``--channel beta``) wire ``headless_blocker_fn`` too."""
    prof = profile_root / "Default"
    (prof / "Preferences.bak.20260101-000000.000001").write_text(
        json.dumps({"restored": True}), encoding="utf-8"
    )
    monkeypatch.setattr(proc_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(proc_mod, "_is_windows", lambda: False)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.1 1111 198.51.100.2 22")
    monkeypatch.setattr(
        proc_mod, "_macos_launchctl_managername", lambda: "Background"
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(
        brave_pkg,
        "find_main_brave_cmdline",
        lambda: ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    )
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda *a, **k: pytest.fail("must NOT close Brave for undo over SSH"),
    )

    undo_args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=None,
        undo=True,
        dry_run=False,
        unattended=False,
        channel="stable",
    )
    with pytest.raises(SystemExit) as exc:
        brave_pkg.cmd_apply(undo_args)
    assert "nothing restored" in str(exc.value)
    assert "was left running" in str(exc.value)

    # Non-stable channel (--channel beta) with Brave closed over SSH:
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proc_mod.BrowserProcess, "running", lambda self: False
    )
    beta_args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(cfg),
        dry_run=False,
        unattended=False,
        channel="beta",
        skip=[],
    )
    with pytest.raises(SystemExit) as exc2:
        brave_pkg.cmd_apply(beta_args)
    assert "[extensions] not applied" in str(exc2.value)
    assert "Brave Beta" in str(exc2.value)

