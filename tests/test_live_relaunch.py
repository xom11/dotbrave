"""Relaunch cho live apply: giữ cờ gốc, và không bỏ rơi trình duyệt đã đóng.

Hai lỗi được đo trên Ubuntu 26.04 (sway, Wayland-only) ngày 09/08/2026:

1. Nhánh live dựng lệnh khởi động lại từ số 0, nên mọi cờ Brave đang chạy
   với nó đều biến mất. Brave mở bằng `--ozone-platform=wayland` bị mở lại
   không có cờ đó, chọn X11, không thấy $DISPLAY và chết ngay -- dotbrave
   vừa đóng trình duyệt vừa không mở lại được. Nhánh offline không dính vì
   nó đã chụp cmdline (`find_cmdline_fn`) trước khi đóng rồi `restart_fn`.

2. `wait_for_devtools_endpoint` thoát bằng SystemExit. Ở lần relaunch SAU
   khi đã ghi xong, việc đó biến một lần apply thành công thành exit khác 0
   và để người dùng không còn trình duyệt nào.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dotbrave._base import live_apply
from dotbrave._base import orchestrator
from dotbrave._base import process as process_mod
from dotbrave._base.utils import Plan
from dotbrave.utils import BROWSER_PROCESS

ORIGINAL_CMDLINE = [
    "/usr/bin/brave-browser",
    "--ozone-platform=wayland",
    "--enable-features=TouchpadOverscrollHistoryNavigation",
]


@pytest.fixture
def prefs_root(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        json.dumps({"brave": {"tabs": {"vertical_tabs_enabled": False}}})
    )
    return tmp_path


def _args(prefs_root: Path, config: Path, **kw) -> argparse.Namespace:
    base = dict(
        profile_root=prefs_root,
        profile="Default",
        config=str(config),
        dry_run=False,
        unattended=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _settings_plan() -> Plan:
    return Plan(
        namespace="settings",
        diff_lines=["  settings: change"],
        apply_fn=lambda prefs: None,
        verify_fn=lambda prefs: None,
    )


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')
    return cfg


# --------------------------------------------------------------------------
# 1. Lệnh relaunch phải mang theo cờ gốc
# --------------------------------------------------------------------------


def test_live_launch_cmdline_forwards_original_flags(tmp_path, monkeypatch):
    """Cờ Brave đang chạy phải có mặt trong lệnh relaunch (nhánh Linux)."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: False)
    monkeypatch.setattr(
        process_mod.shutil, "which", lambda name: "/usr/bin/brave-browser"
    )

    cmdline = BROWSER_PROCESS.live_launch_cmdline(
        tmp_path, "Default", 4321, None, ORIGINAL_CMDLINE
    )

    assert "--ozone-platform=wayland" in cmdline
    assert "--enable-features=TouchpadOverscrollHistoryNavigation" in cmdline
    assert "--remote-debugging-port=4321" in cmdline
    # Cờ dotbrave tự đặt không được nhân đôi từ bản chụp.
    assert sum(a.startswith("--user-data-dir=") for a in cmdline) == 1


def test_live_launch_cmdline_does_not_duplicate_managed_flags(
    tmp_path, monkeypatch
):
    """Bản chụp có sẵn cờ dotbrave quản: giữ bản của dotbrave, không lặp."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: False)
    monkeypatch.setattr(
        process_mod.shutil, "which", lambda name: "/usr/bin/brave-browser"
    )
    captured = [
        "/usr/bin/brave-browser",
        "--user-data-dir=/somewhere/else",
        "--remote-debugging-port=9222",
        "--ozone-platform=wayland",
    ]

    cmdline = BROWSER_PROCESS.live_launch_cmdline(
        tmp_path, "Default", 4321, None, captured
    )

    assert f"--user-data-dir={tmp_path}" in cmdline
    assert "--user-data-dir=/somewhere/else" not in cmdline
    assert "--remote-debugging-port=9222" not in cmdline
    assert "--remote-debugging-port=4321" in cmdline
    assert "--ozone-platform=wayland" in cmdline


def test_relaunch_drops_a_pipe_flag_it_cannot_honour() -> None:
    """Relaunch chạy detached với stdin=DEVNULL, không có fd 3/4: một cờ
    --remote-debugging-pipe được chuyển tiếp khiến tiến trình mới đọc EOF
    và thoát ngay, nên cờ này không bao giờ được nằm trong tập chuyển tiếp."""
    captured = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "--remote-debugging-pipe",
        "--ozone-platform=wayland",
        "--user-data-dir=/somewhere",
    ]

    flags = BROWSER_PROCESS._forwardable_flags(captured)

    assert "--remote-debugging-pipe" not in flags
    assert "--ozone-platform=wayland" in flags, "session flags must still ride along"


def test_relaunch_for_live_apply_captures_cmdline_before_closing(
    prefs_root, tmp_path, monkeypatch
):
    """Brave đang chạy, chưa có endpoint: phải chụp cmdline TRƯỚC khi đóng."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orchestrator, "pick_unused_port", lambda: 9222)
    monkeypatch.setattr(
        orchestrator, "wait_for_devtools_endpoint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        orchestrator, "remember_devtools_port", lambda *a, **k: None
    )
    events: list[str] = []
    seen: list[list[str] | None] = []

    def _launch_live(root, profile, port, url, captured=None):
        events.append("launch")
        seen.append(captured)
        return ["brave-browser"]

    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: (events.append("capture"), ORIGINAL_CMDLINE)[1],
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        live_apply_fn=lambda *a, **k: None,
        graceful_close_fn=lambda: events.append("close"),
        launch_live_fn=_launch_live,
    )

    assert events == ["capture", "close", "launch"]
    assert seen == [ORIGINAL_CMDLINE]


# --------------------------------------------------------------------------
# 2. Relaunch hỏng: mở lại trình duyệt, và đừng nói dối về exit code
# --------------------------------------------------------------------------


def test_failed_relaunch_after_apply_reopens_browser_and_succeeds(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Đã `applied and verified` rồi: relaunch hỏng không được thành exit != 0."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9555)

    def _boom(port, display_name, **kw):
        raise SystemExit("error: Brave did not expose a DevTools endpoint")

    monkeypatch.setattr(orchestrator, "wait_for_devtools_endpoint", _boom)
    restarted: list[list[str]] = []

    def _live_apply(port, prefs_path, prefs, plans, **_kw):
        raise orchestrator.LiveApplyUnsupported(
            browser_name="Brave", keys=["brave.tabs.vertical_tabs_collapsed"]
        )

    up = [True]  # đóng xong là tắt; relaunch chết; lần mở lại thì sống
    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: up[0],
        find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
        restart_fn=lambda c: restarted.append(c) or up.__setitem__(0, True) or c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        live_apply_fn=_live_apply,
        graceful_close_fn=lambda: up.__setitem__(0, False),
        launch_live_fn=lambda *a, **k: ["brave-browser"],
    )

    out = capsys.readouterr()
    assert "ok -- applied and verified" in out.out
    assert restarted == [ORIGINAL_CMDLINE], "phải mở lại Brave bằng cờ gốc"
    assert "was reopened with its original command line." in out.err


# --------------------------------------------------------------------------
# 3. Split apply: nửa live đã chạy rồi, phần còn lại mới đi offline
# --------------------------------------------------------------------------


def test_split_apply_names_only_the_remainder_and_backs_up_once(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Live apply giờ chia theo từng key: nửa làm được đã áp xong trước khi
    ném. Thông báo phải nói đúng như vậy và chỉ liệt kê phần còn lại; cả
    lượt chạy vẫn chỉ được tạo đúng MỘT bản backup (invariant 1), và bản
    đó phải là bản chụp TRƯỚC nửa live -- nếu không `apply --undo` chỉ lùi
    được phần offline."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9555)
    monkeypatch.setattr(
        orchestrator, "wait_for_devtools_endpoint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        orchestrator, "remember_devtools_port", lambda *a, **k: None
    )
    pre_live = (prefs_root / "Default" / "Preferences").read_text()

    def _live_apply(port, prefs_path, prefs, plans, **_kw):
        # A split run, as `live.apply_live` performs it: back up first,
        # then apply the live half (the browser later flushes it into
        # Preferences when the orchestrator closes it), then name the
        # remainder and say the backup is already taken.
        live_apply.backup_preferences(prefs_path)
        prefs_path.write_text(json.dumps({"live": "half"}))
        raise orchestrator.LiveApplyUnsupported(
            browser_name="Brave",
            keys=["brave.tabs.vertical_tabs_collapsed"],
            backup_taken=True,
        )

    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        live_apply_fn=_live_apply,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: ["brave-browser"],
    )

    out = capsys.readouterr().out
    assert "applied everything it could live" in out
    assert "cannot apply every requested setting" not in out
    assert "brave.tabs.vertical_tabs_collapsed" in out
    backups = list((prefs_root / "Default").glob("Preferences.bak.*"))
    assert len(backups) == 1, f"invariant 1: one backup per apply, got {backups}"
    assert backups[0].read_text() == pre_live, (
        "the surviving backup must predate the live half, or --undo cannot "
        "revert it"
    )


def test_offline_fallback_without_a_live_backup_still_takes_one(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Mặt còn lại: live apply chưa kịp backup (không áp được gì live, hoặc
    hỏng trước khi backup) thì nhánh offline vẫn phải tạo đúng một bản."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9555)
    monkeypatch.setattr(
        orchestrator, "wait_for_devtools_endpoint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        orchestrator, "remember_devtools_port", lambda *a, **k: None
    )

    def _live_apply(port, prefs_path, prefs, plans, **_kw):
        raise orchestrator.LiveApplyUnsupported(
            browser_name="Brave", keys=["brave.tabs.vertical_tabs_collapsed"]
        )

    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        live_apply_fn=_live_apply,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: ["brave-browser"],
    )

    backups = list((prefs_root / "Default").glob("Preferences.bak.*"))
    assert len(backups) == 1, f"invariant 1: one backup per apply, got {backups}"


def test_failed_relaunch_before_apply_reopens_browser_then_fails(
    prefs_root, tmp_path, monkeypatch
):
    """Chưa ghi gì mà relaunch hỏng: vẫn phải trả trình duyệt lại, rồi mới lỗi."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orchestrator, "pick_unused_port", lambda: 9222)

    def _boom(port, display_name, **kw):
        raise SystemExit("error: Brave did not expose a DevTools endpoint")

    monkeypatch.setattr(orchestrator, "wait_for_devtools_endpoint", _boom)
    restarted: list[list[str]] = []

    up = [True]
    with pytest.raises(SystemExit):
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: up[0],
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: restarted.append(c) or up.__setitem__(0, True) or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: up.__setitem__(0, False),
            launch_live_fn=lambda *a, **k: ["brave-browser"],
        )

    assert restarted == [ORIGINAL_CMDLINE], "phải mở lại Brave bằng cờ gốc"


# --------------------------------------------------------------------------
# 4. Shell không có display: đừng đóng thứ không mở lại được
# --------------------------------------------------------------------------
#
# Đo trên x13 (labwc, Wayland) ngày 12/11/2026: `dotbrave apply` chạy từ một
# shell không có $WAYLAND_DISPLAY lẫn $DISPLAY đóng Brave, relaunch chết ngay
# ("Missing X server or $DISPLAY", 0,16 giây với Chromium), lần mở lại bằng
# cmdline gốc cũng chết y vậy -- vậy mà vẫn in "was reopened with its
# original command line." Sau đó không còn tiến trình Brave nào.
#
# 0.7.1 đọc display từ /proc/<pid>/environ của chính Brave và vẫn hỏng y
# vậy: setproctitle của Chromium ghi đè vùng đó, Brave 1.98 đọc ra 14 KB
# toàn NUL (cả tiến trình con cũng thế). 0.7.1 coi `{}` là "headless" nên
# không chặn. Môi trường thật nằm ở tiến trình cha (`.brave-wrapped`).

on_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc"
)


def _linux_proc(monkeypatch, session_env):
    """BrowserProcess mới (không dùng chung BROWSER_PROCESS), giả làm Linux
    với một tiến trình chính có môi trường `session_env`."""
    from dotbrave.utils import _make_browser_process

    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: False)
    proc = _make_browser_process("stable")
    monkeypatch.setattr(proc, "pids", lambda: ["4242"])
    monkeypatch.setattr(process_mod, "_read_cmdline", lambda pid: ORIGINAL_CMDLINE)
    monkeypatch.setattr(process_mod, "_find_session_env", lambda pid: session_env)
    return proc


def test_relaunch_borrows_the_display_of_the_browser_it_closes(monkeypatch):
    """Shell thiếu display: relaunch lấy display từ chính Brave đang chạy,
    nhưng giữ nguyên biến shell đã có."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/from-shell")
    proc = _linux_proc(
        monkeypatch,
        {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000"},
    )
    spawned: list[dict | None] = []
    monkeypatch.setattr(
        process_mod.subprocess, "Popen",
        lambda cmd, **kw: spawned.append(kw.get("env")),
    )
    monkeypatch.setattr(process_mod.shutil, "which", lambda name: None)

    assert proc.find_main_cmdline() == ORIGINAL_CMDLINE
    assert proc.relaunch_blocker() is None
    proc.restart(ORIGINAL_CMDLINE)

    env = spawned[0]
    assert env is not None and env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/from-shell"


LABWC_SESSION = {
    "WAYLAND_DISPLAY": "wayland-0",
    "DISPLAY": ":0",
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "labwc:wlroots",
    "NIXOS_OZONE_WL": "1",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}


def _chromium_picks(env: dict) -> str:
    """`SetOzonePlatformForLinuxIfNeeded` (ui/linux/display_server_utils.cc)
    với `--ozone-platform-hint=auto`: chỉ một bên kết nối được thì dùng bên
    đó; cả hai thì Wayland chỉ khi XDG_SESSION_TYPE == "wayland"."""
    use_wayland = env.get("XDG_SESSION_TYPE") == "wayland"
    has_wayland = bool(env.get("WAYLAND_DISPLAY") or env.get("WAYLAND_SOCKET"))
    has_x11 = bool(env.get("DISPLAY"))
    if has_wayland != has_x11:
        use_wayland = has_wayland
    return "wayland" if use_wayland else "x11"


def test_agent_shell_relaunch_lands_on_wayland_not_xwayland(monkeypatch):
    """Đo trên x13 với 0.7.2: shell của agent có XDG_SESSION_TYPE=tty, không
    display; 0.7.2 chỉ bù biến thiếu nên giữ `tty`, mượn cả WAYLAND_DISPLAY
    lẫn DISPLAY -- Chromium chọn x11, Brave mờ trên XWayland."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/from-shell")
    proc = _linux_proc(monkeypatch, dict(LABWC_SESSION))
    proc.find_main_cmdline()

    fill_only = {
        **os.environ,
        **{k: v for k, v in LABWC_SESSION.items() if not os.environ.get(k)},
    }
    assert _chromium_picks(fill_only) == "x11", "the 0.7.2 rule, as measured"

    env = proc._launch_env()
    assert _chromium_picks(env) == "wayland"
    assert env["XDG_SESSION_TYPE"] == "wayland"
    assert env["XDG_CURRENT_DESKTOP"] == "labwc:wlroots"
    assert env["NIXOS_OZONE_WL"] == "1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/from-shell"
    assert env["PATH"] == os.environ["PATH"], "the rest stays ours"


def test_forwarded_x_display_of_the_shell_is_dropped(monkeypatch):
    """SSH -X: DISPLAY của shell là màn hình bên kia đường ống. Bộ biến
    display đi nguyên từ phiên của Brave -- có thì đặt, không có thì bỏ."""
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.setenv("WAYLAND_SOCKET", "3")
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    proc = _linux_proc(
        monkeypatch, {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"}
    )
    proc.find_main_cmdline()

    env = proc._launch_env()
    assert "DISPLAY" not in env and "WAYLAND_SOCKET" not in env
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert _chromium_picks(env) == "wayland"


def test_session_type_is_inferred_when_an_older_relaunch_poisoned_it(monkeypatch):
    """Brave do 0.7.2 relaunch mang `tty` trong chính phiên của nó
    (.brave-wrapped thừa kế từ shell). Chép lại thì lần sau cũng XWayland."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    poisoned = {**LABWC_SESSION, "XDG_SESSION_TYPE": "tty"}
    proc = _linux_proc(monkeypatch, poisoned)
    proc.find_main_cmdline()
    assert proc._launch_env()["XDG_SESSION_TYPE"] == "wayland"

    x11_only = {"DISPLAY": ":1"}
    proc = _linux_proc(monkeypatch, x11_only)
    proc.find_main_cmdline()
    env = proc._launch_env()
    assert env["XDG_SESSION_TYPE"] == "x11" and "WAYLAND_DISPLAY" not in env


def test_same_session_terminal_changes_nothing(monkeypatch):
    """Terminal trong chính phiên desktop: giá trị trùng nhau, không đổi gì."""
    for key, value in LABWC_SESSION.items():
        monkeypatch.setenv(key, value)
    proc = _linux_proc(monkeypatch, dict(LABWC_SESSION))
    proc.find_main_cmdline()

    assert proc._launch_env() == dict(os.environ)
    assert proc.relaunch_blocker() is None


def test_no_display_anywhere_blocks_the_close(monkeypatch):
    """Không tìm được display ở Brave lẫn tiến trình cha, shell cũng không
    có: chặn."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    proc = _linux_proc(monkeypatch, None)
    proc.find_main_cmdline()

    reason = proc.relaunch_blocker()
    assert reason is not None
    assert "$WAYLAND_DISPLAY" in reason and "offline" in reason


def test_only_a_headless_command_line_exempts_a_browser_without_display(
    monkeypatch,
):
    """Môi trường rỗng không chứng minh gì (setproctitle); chỉ cmdline
    headless mới được miễn."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    proc = _linux_proc(monkeypatch, None)
    proc.find_main_cmdline()
    assert proc.relaunch_blocker() is not None

    monkeypatch.setattr(
        process_mod, "_read_cmdline",
        lambda pid: [*ORIGINAL_CMDLINE, "--ozone-platform=headless"],
    )
    proc.find_main_cmdline()
    assert proc.relaunch_blocker() is None


def test_session_env_comes_from_the_launcher_when_the_browser_is_blank(
    monkeypatch,
):
    """Brave đọc ra rỗng (setproctitle): lấy từ tiến trình cha gần nhất có
    display. Cây tiến trình giả hoàn toàn -- trong sandbox build của Nix,
    cha của pytest là pid 1 và uid là nixbld."""
    parents = {"300": "200", "200": "100", "100": "1"}
    owners = {"300", "200"}
    envs = {"300": {}, "200": {"WAYLAND_DISPLAY": "wayland-9"}, "100": {}}
    monkeypatch.setattr(process_mod, "_parent_pid", parents.get)
    monkeypatch.setattr(process_mod, "_owned_by_us", lambda pid: pid in owners)
    monkeypatch.setattr(
        process_mod, "_read_session_env", lambda pid: envs.get(pid, {})
    )

    assert process_mod._find_session_env("300") == {"WAYLAND_DISPLAY": "wayland-9"}

    # Không ancestor nào có display, hoặc display nằm sau một tiến trình
    # của user khác / sau pid 1: không đoán.
    envs["200"] = {"XDG_SESSION_TYPE": "wayland"}
    assert process_mod._find_session_env("300") is None
    envs["100"] = {"WAYLAND_DISPLAY": "wayland-0"}
    assert process_mod._find_session_env("300") is None, "100 is not ours"
    owners.add("100")
    assert process_mod._find_session_env("300") == {"WAYLAND_DISPLAY": "wayland-0"}
    parents["100"] = "1"
    envs["1"] = {"WAYLAND_DISPLAY": "wayland-1"}
    envs["100"] = {}
    assert process_mod._find_session_env("300") is None, "never past pid 1"


@on_linux
def test_singleton_lock_held_only_by_a_live_local_pid(tmp_path):
    import socket

    lock = tmp_path / "SingletonLock"
    host = socket.gethostname()
    assert process_mod.singleton_lock_held(tmp_path) is False
    lock.symlink_to(f"{host}-{os.getpid()}")
    assert process_mod.singleton_lock_held(tmp_path) is True
    lock.unlink()
    child = subprocess.Popen(["true"])
    child.wait()
    lock.symlink_to(f"{host}-{child.pid}")
    assert process_mod.singleton_lock_held(tmp_path) is False, "stale lock"
    lock.unlink()
    lock.symlink_to(f"another-host-{os.getpid()}")
    assert process_mod.singleton_lock_held(tmp_path) is False


@on_linux
def test_read_session_env_keeps_only_session_keys():
    env = process_mod._read_session_env(str(os.getpid()))
    assert env is not None
    assert set(env) <= set(process_mod._SESSION_ENV_KEYS)
    assert process_mod._read_session_env("0") is None


@on_linux
def test_dead_relaunch_zombie_is_not_counted_as_running(monkeypatch):
    """`pgrep -x` liệt kê cả zombie, và cmdline rỗng của zombie bị phạm vi
    profile đọc thành một lần mở flagless -- relaunch đã chết vẫn bị coi là
    đang chạy."""
    child = subprocess.Popen(["true"])
    deadline = time.monotonic() + 5
    while not process_mod._is_zombie(str(child.pid)):
        assert time.monotonic() < deadline, "child never became a zombie"
        time.sleep(0.01)
    from dotbrave.utils import _make_browser_process

    proc = _make_browser_process("stable")
    monkeypatch.setattr(
        process_mod.subprocess, "check_output",
        lambda *a, **k: f"{child.pid}\n".encode(),
    )
    try:
        assert proc.running() is False
    finally:
        child.wait()
    assert process_mod._is_zombie(str(child.pid)) is False


def test_apply_leaves_browser_running_when_relaunch_cannot_work(
    prefs_root, tmp_path, monkeypatch
):
    """Nhánh live bootstrap: bị chặn thì không đóng, không launch, exit != 0."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    events: list[str] = []

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: events.append("restart") or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: events.append("live"),
            graceful_close_fn=lambda: events.append("close"),
            launch_live_fn=lambda *a, **k: events.append("launch") or ["b"],
            close_blocker_fn=lambda: "no display here",
        )

    assert events == []
    assert "left running: no display here" in str(exc.value)
    assert "[settings] not applied" in str(exc.value)


def test_offline_apply_leaves_browser_running_when_relaunch_cannot_work(
    prefs_root, tmp_path
):
    """Nhánh offline: cũng vậy, và Preferences không bị đụng tới."""
    before = (prefs_root / "Default" / "Preferences").read_bytes()
    events: list[str] = []

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: events.append("restart") or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            graceful_close_fn=lambda: events.append("close"),
            close_blocker_fn=lambda: "no display here",
        )

    assert events == []
    assert "left running" in str(exc.value)
    assert (prefs_root / "Default" / "Preferences").read_bytes() == before
    assert not list((prefs_root / "Default").glob("Preferences.bak.*"))


def test_reopen_that_dies_is_reported_not_claimed(
    prefs_root, tmp_path, monkeypatch
):
    """Mở lại xong mà Brave chết ngay: nói thật, không in câu "was reopened"."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orchestrator, "pick_unused_port", lambda: 9222)

    def _boom(port, display_name, **kw):
        raise SystemExit("error: Brave did not expose a DevTools endpoint")

    monkeypatch.setattr(orchestrator, "wait_for_devtools_endpoint", _boom)
    restarted: list[list[str]] = []
    up = [True]

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: up[0],
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: restarted.append(c) or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: up.__setitem__(0, False),
            launch_live_fn=lambda *a, **k: ["brave-browser"],
        )

    message = str(exc.value)
    assert restarted == [ORIGINAL_CMDLINE]
    assert "is not running" in message and "restart it manually" in message
    assert "Brave was reopened with its original command line." not in message


def test_restart_that_dies_after_apply_warns_and_exits_zero(
    prefs_root, tmp_path, capsys
):
    """Đã `applied and verified`: Brave không sống lại là cảnh báo trên
    stderr, exit 0 (invariant 5) -- nhưng phải nói ra."""
    restarted: list[list[str]] = []

    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: not restarted,
        find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
        restart_fn=lambda c: restarted.append(c) or c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        graceful_close_fn=lambda: None,
    )

    out = capsys.readouterr()
    assert "ok -- applied and verified" in out.out
    assert "is not running" in out.err and "start it manually" in out.err


def _bootstrap_that_times_out(monkeypatch):
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orchestrator, "pick_unused_port", lambda: 9222)

    def _boom(port, display_name, **kw):
        raise SystemExit("error: Brave did not expose a DevTools endpoint")

    monkeypatch.setattr(orchestrator, "wait_for_devtools_endpoint", _boom)
    monkeypatch.setattr(orchestrator, "_PREVIOUS_EXIT_TIMEOUT_S", 5.0)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)


def test_reopen_waits_for_the_failed_relaunch_to_finish_exiting(
    prefs_root, tmp_path, monkeypatch
):
    """Mở lại khi bản relaunch còn đang thoát thì lệnh mới trao cmdline cho
    nó qua singleton rồi chết theo -- phải đợi nó thoát hẳn đã."""
    _bootstrap_that_times_out(monkeypatch)
    events: list[str] = []
    phase = {"state": "up", "exiting_polls": 3}

    def _running():
        if phase["state"] == "exiting":
            phase["exiting_polls"] -= 1
            if phase["exiting_polls"] <= 0:
                phase["state"] = "gone"
            events.append("poll")
            return True
        return phase["state"] in ("up", "reopened")

    def _launch(*a, **k):
        phase["state"] = "exiting"
        return ["brave-browser"]

    def _restart(c):
        events.append("restart")
        phase["state"] = "reopened"
        return c

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=_running,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=_restart,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: phase.__setitem__("state", "gone"),
            launch_live_fn=_launch,
        )

    assert events == ["poll", "poll", "poll", "restart"]
    assert "Brave was reopened with its original command line." in str(exc.value)


def test_relaunch_that_survived_without_endpoint_is_not_reopened_twice(
    prefs_root, tmp_path, monkeypatch
):
    """Bản relaunch vẫn sống (chỉ là không có endpoint): đó là trình duyệt
    của người dùng rồi, đừng mở thêm cái nữa."""
    _bootstrap_that_times_out(monkeypatch)
    monkeypatch.setattr(orchestrator, "_PREVIOUS_EXIT_TIMEOUT_S", 0.0)
    restarted: list[list[str]] = []

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: restarted.append(c) or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: None,
            launch_live_fn=lambda *a, **k: ["brave-browser"],
        )

    assert restarted == []
    assert "running again" in str(exc.value)


@on_linux
def test_reopen_waits_for_a_live_singleton_lock_to_go(
    prefs_root, tmp_path, monkeypatch
):
    """Không còn pid nào trong phạm vi nhưng SingletonLock vẫn do một tiến
    trình sống giữ: vẫn phải đợi."""
    import socket

    _bootstrap_that_times_out(monkeypatch)
    lock = prefs_root / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    polls = {"n": 0}
    restarted: list[bool] = []

    def _running():
        if not restarted:
            polls["n"] += 1
            if polls["n"] == 3 and lock.is_symlink():
                lock.unlink()
            return polls["n"] == 1  # chỉ lần kiểm tra đầu: Brave đang chạy
        return True

    with pytest.raises(SystemExit):
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=_running,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: restarted.append(lock.is_symlink()) or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: None,
            launch_live_fn=lambda *a, **k: ["brave-browser"],
        )

    assert restarted == [False], "restart must wait for the lock to go"
