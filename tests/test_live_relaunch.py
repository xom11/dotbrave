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

    orchestrator.cmd_apply(
        _args(prefs_root, _config(tmp_path)),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
        restart_fn=lambda c: restarted.append(c) or c,
        build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
        live_apply_fn=_live_apply,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: ["brave-browser"],
    )

    out = capsys.readouterr()
    assert "ok -- applied and verified" in out.out
    assert restarted == [ORIGINAL_CMDLINE], "phải mở lại Brave bằng cờ gốc"


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

    with pytest.raises(SystemExit):
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
    monkeypatch.setattr(process_mod, "_read_session_env", lambda pid: session_env)
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


def test_relaunch_keeps_this_shells_env_when_it_has_a_display(monkeypatch):
    """Shell có display sẵn: không đụng vào môi trường của relaunch."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    proc = _linux_proc(monkeypatch, {"WAYLAND_DISPLAY": "wayland-1"})
    proc.find_main_cmdline()

    assert proc._launch_env() is None
    assert proc.relaunch_blocker() is None


def test_no_display_anywhere_blocks_the_close(monkeypatch):
    """Không đọc được môi trường của Brave, shell không có display: chặn."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    proc = _linux_proc(monkeypatch, None)
    proc.find_main_cmdline()

    reason = proc.relaunch_blocker()
    assert reason is not None
    assert "$WAYLAND_DISPLAY" in reason and "offline" in reason


def test_headless_browser_without_display_is_not_blocked(monkeypatch):
    """Đọc được môi trường mà chính Brave cũng không có display (headless):
    mở lại giống hệt thì không tệ hơn, nên không chặn."""
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    proc = _linux_proc(monkeypatch, {})
    proc.find_main_cmdline()

    assert proc.relaunch_blocker() is None


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

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_apply(
            _args(prefs_root, _config(tmp_path)),
            display_name="Brave",
            running_fn=lambda: not restarted,
            find_cmdline_fn=lambda: ORIGINAL_CMDLINE,
            restart_fn=lambda c: restarted.append(c) or c,
            build_plans_fn=lambda p, prefs, doc, **kw: [_settings_plan()],
            live_apply_fn=lambda *a, **k: None,
            graceful_close_fn=lambda: None,
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
