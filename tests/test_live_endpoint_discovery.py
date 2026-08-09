"""Tìm và dựng lại endpoint: nguồn dò, cách khởi động macOS, thứ tự log.

Ba thứ đo được trên máy thật ngày 09/08/2026:

1. Brave chỉ ghi ``DevToolsActivePort`` khi ``--remote-debugging-port=0``.
   Với một port cố định file không xuất hiện -- trên cả macOS lẫn Linux --
   nên một Brave đang chạy sẵn endpoint vẫn bị coi là không có, và bị đóng.
   Command line của tiến trình biết port đó, và đã đọc được sẵn.

2. ``open -a … --args`` trên macOS không đáng tin ngay sau một lần đóng:
   cùng một lệnh, lần đầu args bị nuốt (endpoint không bao giờ lên), lần
   sau chạy tốt. Gọi thẳng binary trong bundle là deterministic.

3. Cảnh báo ghi ra stderr xuất hiện TRƯỚC phần plan ở stdout trong log
   `darwin-rebuild`, vì stdout bị block-buffer qua pipe. Đọc log thành ra
   thấy "not applied" rồi thấy plan bên dưới, dễ tưởng plan đã được ghi.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dotbrave._base import cdp
from dotbrave._base import orchestrator
from dotbrave._base import process as process_mod
from dotbrave.utils import BROWSER_PROCESS

MAC_EXE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"


# --------------------------------------------------------------------------
# 1. Dò endpoint từ command line của tiến trình đang chạy
# --------------------------------------------------------------------------


def test_live_port_from_cmdline_reads_the_running_command_line(monkeypatch):
    """Không có sidecar, không có DevToolsActivePort -- còn cmdline."""
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: port == 8123)

    port = cdp.live_port_from_cmdline(
        lambda: [MAC_EXE, "--remote-debugging-port=8123"]
    )

    assert port == 8123


def test_live_port_from_cmdline_ignores_a_dead_port(monkeypatch):
    """Cờ còn đó nhưng endpoint đã chết: coi như không có."""
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: False)

    assert (
        cdp.live_port_from_cmdline(
            lambda: [MAC_EXE, "--remote-debugging-port=8123"]
        )
        is None
    )


def test_live_port_from_cmdline_skips_a_dynamic_port(monkeypatch):
    """`--remote-debugging-port=0` nghĩa là số thật nằm ở DevToolsActivePort."""
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: True)

    assert cdp.live_port_from_cmdline(lambda: [MAC_EXE, "--remote-debugging-port=0"]) is None


def test_live_port_from_cmdline_survives_an_unreadable_command_line(monkeypatch):
    """Đọc cmdline hỏng không được làm chết cả lần apply."""
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: True)

    def _boom():
        raise OSError("no such process")

    assert cdp.live_port_from_cmdline(_boom) is None
    assert cdp.live_port_from_cmdline(None) is None


def test_find_devtools_port_signature_is_unchanged(tmp_path):
    """Nguồn trên đĩa vẫn đứng riêng, người gọi cũ không phải biết gì thêm."""
    assert cdp.find_devtools_port(tmp_path, "Default") is None


def test_orchestrator_offers_the_running_cmdline_to_the_port_lookup(
    tmp_path, monkeypatch
):
    """cmd_apply phải truyền find_cmdline_fn xuống, không thì fix vô dụng."""
    import argparse
    import json

    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"a": {"b": False}}))
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    seen: list = []

    def _from_cmdline(cmdline_fn):
        seen.append(cmdline_fn() if cmdline_fn else None)
        return 9555

    # Không có gì trên đĩa: đúng tình huống Brave chạy với port cố định.
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "live_port_from_cmdline", _from_cmdline)
    monkeypatch.setattr(orchestrator, "remember_devtools_port", lambda *a, **k: None)

    orchestrator.cmd_apply(
        argparse.Namespace(
            profile_root=tmp_path, profile="Default", config=str(cfg),
            dry_run=False, unattended=False,
        ),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: [MAC_EXE, "--remote-debugging-port=9555"],
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            orchestrator.Plan(
                namespace="settings",
                diff_lines=["  settings: change"],
                apply_fn=lambda prefs: None,
                verify_fn=lambda prefs: None,
            )
        ],
        live_apply_fn=lambda *a: None,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: [],
    )

    assert seen == [[MAC_EXE, "--remote-debugging-port=9555"]]


# --------------------------------------------------------------------------
# 1b. `ps` và Win32_Process trả MỘT chuỗi, không phải argv
# --------------------------------------------------------------------------

FLAT_MAC_CMDLINE = (
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser "
    "--user-data-dir=/Users/x/Library/Application Support/BraveSoftware/Brave-Browser "
    "--profile-directory=Default --remote-debugging-port=52831"
)


def test_read_cmdline_splits_the_flat_macos_string(monkeypatch):
    """`ps -o command=` in ra một dòng; người gọi cần từng token.

    Không dùng shlex được: `ps` không quote, và cả đường dẫn app lẫn
    `Application Support` đều có khoảng trắng -- shlex sẽ cắt chúng làm
    đôi. Chromium luôn viết `--flag=value`, nên ` --` là ranh giới đáng
    tin duy nhất.
    """
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(
        process_mod.subprocess,
        "check_output",
        lambda *a, **k: (FLAT_MAC_CMDLINE + "\n").encode(),
    )

    argv = process_mod._read_cmdline("123")

    assert argv[0] == "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    assert (
        "--user-data-dir=/Users/x/Library/Application Support/BraveSoftware/Brave-Browser"
        in argv
    )
    assert "--profile-directory=Default" in argv
    assert "--remote-debugging-port=52831" in argv


def test_forwardable_flags_work_on_a_real_macos_command_line(monkeypatch):
    """Chuỗi `ps` đã tách rồi thì cờ mới forward được."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(
        process_mod.subprocess,
        "check_output",
        lambda *a, **k: (
            FLAT_MAC_CMDLINE + " --ozone-platform=wayland\n"
        ).encode(),
    )
    argv = process_mod._read_cmdline("123")

    flags = BROWSER_PROCESS._forwardable_flags(argv)

    assert "--ozone-platform=wayland" in flags
    # cờ dotbrave tự quản không được lọt vào
    assert not any(f.startswith("--user-data-dir") for f in flags)
    assert not any(f.startswith("--remote-debugging-port") for f in flags)


def test_live_port_is_found_from_a_real_macos_command_line(monkeypatch):
    """Đường đi đầy đủ: ps → tách → tìm port."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(
        process_mod.subprocess,
        "check_output",
        lambda *a, **k: (FLAT_MAC_CMDLINE + "\n").encode(),
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: port == 52831)

    port = cdp.live_port_from_cmdline(lambda: process_mod._read_cmdline("123"))

    assert port == 52831


# --------------------------------------------------------------------------
# 2. macOS: khởi động bằng binary trong bundle, không qua `open -a`
# --------------------------------------------------------------------------


def test_macos_live_launch_uses_the_running_binary(tmp_path, monkeypatch):
    """Biết Brave đang chạy từ đâu thì dùng đúng binary đó."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)

    cmdline = BROWSER_PROCESS.live_launch_cmdline(
        tmp_path, "Default", 4321, None, [MAC_EXE, "--enable-features=Foo"]
    )

    assert cmdline[0] == MAC_EXE
    assert "open" not in cmdline
    assert "--args" not in cmdline
    assert "--remote-debugging-port=4321" in cmdline
    assert "--enable-features=Foo" in cmdline


def test_macos_live_launch_finds_the_bundle_when_nothing_is_running(
    tmp_path, monkeypatch
):
    """Brave đã đóng sẵn: dò bundle trong các thư mục Applications."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)
    app = tmp_path / f"{BROWSER_PROCESS.macos_app_name}.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    exe = app / BROWSER_PROCESS.macos_app_name
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(process_mod, "_MACOS_APP_DIRS", (tmp_path,))

    cmdline = BROWSER_PROCESS.live_launch_cmdline(tmp_path, "Default", 4321, None, None)

    assert cmdline[0] == str(exe)
    assert "open" not in cmdline


def test_macos_live_launch_falls_back_to_open_when_no_bundle_found(
    tmp_path, monkeypatch
):
    """Không tìm ra bundle: vẫn còn đường cũ, không nổ."""
    monkeypatch.setattr(process_mod, "_is_windows", lambda: False)
    monkeypatch.setattr(process_mod, "_is_macos", lambda: True)
    monkeypatch.setattr(process_mod, "_MACOS_APP_DIRS", (tmp_path / "nowhere",))

    cmdline = BROWSER_PROCESS.live_launch_cmdline(tmp_path, "Default", 4321, None, None)

    assert cmdline[:2] == ["open", "-a"]
    assert "--args" in cmdline


# --------------------------------------------------------------------------
# 3. stdout phải được flush trước khi ghi stderr
# --------------------------------------------------------------------------


class _Recorder:
    """stdout/stderr giả, ghi lại thứ tự thao tác chứ không chỉ nội dung."""

    def __init__(self, events: list, name: str) -> None:
        self.events = events
        self.name = name

    def write(self, s: str) -> int:
        if s.strip():
            self.events.append((self.name, s.strip()))
        return len(s)

    def flush(self) -> None:
        self.events.append(("flush", self.name))


def test_warn_flushes_stdout_before_writing_stderr(monkeypatch):
    events: list = []
    monkeypatch.setattr(sys, "stdout", _Recorder(events, "stdout"))
    monkeypatch.setattr(sys, "stderr", _Recorder(events, "stderr"))

    orchestrator._warn("nothing above was written")

    assert events == [
        ("flush", "stdout"),
        ("stderr", "nothing above was written"),
    ]


def test_unattended_skip_keeps_plan_and_warning_in_order(tmp_path, monkeypatch):
    """Log gộp phải đọc được: plan trước, rồi mới tới dòng 'not applied'."""
    import argparse
    import json

    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"a": {"b": False}}))
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    monkeypatch.setattr(
        orchestrator, "find_devtools_port", lambda *a, **k: None
    )
    events: list = []
    monkeypatch.setattr(sys, "stdout", _Recorder(events, "stdout"))
    monkeypatch.setattr(sys, "stderr", _Recorder(events, "stderr"))

    orchestrator.cmd_apply(
        argparse.Namespace(
            profile_root=tmp_path, profile="Default", config=str(cfg),
            dry_run=False, unattended=True,
        ),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            orchestrator.Plan(
                namespace="settings",
                diff_lines=["  settings: change"],
                apply_fn=lambda prefs: None,
                verify_fn=lambda prefs: None,
            )
        ],
        live_apply_fn=lambda *a: None,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: [],
    )

    kinds = [e[0] for e in events]
    assert "stderr" in kinds, "phải có cảnh báo"
    # mọi thứ ghi ra stdout phải nằm trước cảnh báo đầu tiên ở stderr,
    # và ngay trước nó phải là một lần flush.
    first_err = kinds.index("stderr")
    assert "stdout" in kinds[:first_err], "plan phải in ra trước"
    assert kinds[first_err - 1] == "flush", "phải flush stdout ngay trước stderr"
