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
from pathlib import Path

import pytest

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
        live_apply_fn=lambda *a: None,
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

    def _live_apply(port, prefs_path, prefs, plans):
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
            live_apply_fn=lambda *a: None,
            graceful_close_fn=lambda: None,
            launch_live_fn=lambda *a, **k: ["brave-browser"],
        )

    assert restarted == [ORIGINAL_CMDLINE], "phải mở lại Brave bằng cờ gốc"
