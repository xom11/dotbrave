"""«Không biết Brave có chạy không» phải khác hẳn «Brave không chạy».

Bug thật, bắt được trong một `darwin-rebuild switch`: script activation của
home-manager export một PATH chỉ gồm store path, không có `/usr/bin`. Trên
macOS `pgrep` nằm ở `/usr/bin/pgrep`, nên `subprocess` ném `FileNotFoundError`
-- và chỗ bắt lỗi cũ gộp nó chung với `CalledProcessError`, trả về `[]`.
`running()` thành `False` trong khi Brave đang chạy thật, `apply` đi nhánh
offline: backup, ghi Preferences, đọc lại, verify xanh, in
"ok -- applied and verified". Brave vẫn giữ Preferences trong bộ nhớ nên nó
flush bản của mình đè lên -- không một phím tắt nào đổi. Mỗi lần rebuild lại
đẻ thêm một file `.bak` ~200 KB.

Mọi lớp bảo vệ của `--unattended` đều nằm SAU `running_fn()` trả True, nên
không lớp nào kịp chạy.

Hai test cuối phần orchestrator là hai test lẽ ra đã bắt được lỗi này: chúng
chạy qua một `BrowserProcess` thật với `pgrep` bị làm cho biến mất, chứ không
giả lập bằng một callback ném sẵn exception.
"""
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from dotbrave._base import orchestrator
from dotbrave._base import process as bp
from dotbrave._base.utils import Plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc(**kw) -> bp.BrowserProcess:
    base = dict(
        display_name="Brave",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser",
        linux_wrappers=["brave-browser"],
        windows_exe_relpath=(
            "BraveSoftware", "Brave-Browser", "Application", "brave.exe",
        ),
    )
    base.update(kw)
    return bp.BrowserProcess(**base)


def _tool_missing(cmd, **kw):
    """What `subprocess` does when argv[0] isn't anywhere on PATH."""
    raise FileNotFoundError(2, "No such file or directory", cmd[0])


def _no_match(cmd, **kw):
    """What `pgrep`/`tasklist` do when nothing matched: exit non-zero."""
    raise subprocess.CalledProcessError(1, cmd)


# ---------------------------------------------------------------------------
# process.py -- the two failures must not look alike
# ---------------------------------------------------------------------------

def test_running_raises_when_pgrep_is_missing(monkeypatch) -> None:
    """Công cụ không có = không biết. Tuyệt đối không phải "không chạy"."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)

    with pytest.raises(bp.ProcessStateUnknown) as excinfo:
        _proc().running()

    assert excinfo.value.tool == "pgrep"
    assert "pgrep" in str(excinfo.value)


def test_running_is_false_when_pgrep_matches_nothing(monkeypatch) -> None:
    """`pgrep` exit != 0 vì không khớp process nào -- đó là câu trả lời thật."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _no_match)

    proc = _proc()
    assert proc.running() is False
    assert proc.pids() == []


def test_running_is_true_when_pgrep_matches(monkeypatch) -> None:
    """Không hồi quy đường thường: có pid thì vẫn True."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: b"100\n200\n"
    )

    proc = _proc()
    assert proc.running() is True
    assert proc.pids() == ["100", "200"]


def test_pids_stays_best_effort_when_pgrep_is_missing(monkeypatch) -> None:
    """`pids()` là danh sách nỗ-lực-tối-đa: mọi caller đều lặp qua kết quả và
    "không làm gì" là hành vi đúng khi không liệt kê được. Nó KHÔNG được bắt
    đầu ném exception, chỉ `running()` mới ném."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)

    proc = _proc()
    assert proc.pids() == []
    assert proc.find_main_cmdline() is None


def test_running_raises_when_tasklist_is_missing(monkeypatch) -> None:
    """Windows đi qua `tasklist` nhưng lập luận y hệt."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)

    with pytest.raises(bp.ProcessStateUnknown) as excinfo:
        _proc().running()

    assert excinfo.value.tool == "tasklist"


def test_running_is_false_when_tasklist_matches_nothing(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(bp.subprocess, "check_output", _no_match)

    proc = _proc()
    assert proc.running() is False
    assert proc.pids() == []


# ---------------------------------------------------------------------------
# orchestrator.py -- apply must refuse to guess
# ---------------------------------------------------------------------------

_UNTOUCHED = {"some": {"unrelated": "preference"}}


@pytest.fixture
def prefs_root(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps(_UNTOUCHED))
    return tmp_path


def _args(prefs_root: Path, config: Path, **kw) -> argparse.Namespace:
    base = dict(
        profile_root=prefs_root,
        profile="Default",
        config=str(config),
        dry_run=False,
        unattended=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _writing_plan(applied: list) -> Plan:
    """A plan whose write is visible on disk, so "nothing was written" is a
    claim the test can actually check."""

    def apply_fn(prefs: dict) -> None:
        prefs["dotbrave_wrote_this"] = True
        applied.append("settings")

    return Plan(
        namespace="settings",
        diff_lines=['  "a.b": false -> true'],
        apply_fn=apply_fn,
        verify_fn=lambda prefs: None,
    )


def _run_apply(prefs_root: Path, tmp_path: Path, applied: list, **kw) -> None:
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')
    proc = _proc()
    orchestrator.cmd_apply(
        _args(prefs_root, cfg, **kw),
        display_name="Brave",
        running_fn=proc.running,
        find_cmdline_fn=proc.find_main_cmdline,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **k: [_writing_plan(applied)],
        live_apply_fn=lambda *a, **k: None,
        graceful_close_fn=lambda: pytest.fail("must not close Brave"),
        launch_live_fn=lambda *a: [],
    )


def _assert_profile_untouched(prefs_root: Path) -> None:
    profile = prefs_root / "Default"
    assert list(profile.glob("Preferences.bak.*")) == [], (
        "a backup was taken for a write that never happened"
    )
    assert json.loads((profile / "Preferences").read_text()) == _UNTOUCHED


def test_unattended_skips_when_running_state_is_unknown(
    prefs_root, tmp_path, monkeypatch, capsys
) -> None:
    """`--unattended` + không biết Brave có chạy không: báo stderr, bỏ qua,
    exit 0. Không backup, không ghi gì -- đúng hợp đồng của cờ này."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)
    applied: list = []

    _run_apply(prefs_root, tmp_path, applied)

    assert applied == []
    _assert_profile_untouched(prefs_root)
    out, err = capsys.readouterr()
    assert "unattended" in err
    assert "pgrep" in err
    assert "ok -- applied and verified" not in out


def test_attended_fails_loudly_when_running_state_is_unknown(
    prefs_root, tmp_path, monkeypatch
) -> None:
    """Không có `--unattended`: hỏng to, nêu đích danh công cụ thiếu và nói rõ
    vì sao ghi offline lúc này là ghi rồi mất. Vẫn không backup, không ghi."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)
    applied: list = []

    with pytest.raises(SystemExit) as excinfo:
        _run_apply(prefs_root, tmp_path, applied, unattended=False)

    msg = str(excinfo.value)
    assert "pgrep" in msg
    assert "undone" in msg
    assert applied == []
    _assert_profile_untouched(prefs_root)


def test_offline_apply_still_writes_when_brave_is_confirmed_closed(
    prefs_root, tmp_path, monkeypatch, capsys
) -> None:
    """Không hồi quy: `pgrep` có mặt và không khớp gì = Brave đóng thật, nên
    đường offline vẫn backup + ghi + verify như trước."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _no_match)
    applied: list = []

    _run_apply(prefs_root, tmp_path, applied)

    profile = prefs_root / "Default"
    assert applied == ["settings"]
    assert len(list(profile.glob("Preferences.bak.*"))) == 1
    written = json.loads((profile / "Preferences").read_text())
    assert written["dotbrave_wrote_this"] is True
    assert "ok -- applied and verified" in capsys.readouterr().out


def test_guard_survives_a_reload_of_the_process_module(
    prefs_root, tmp_path, monkeypatch, capsys
) -> None:
    """`test_platform.py` gọi `importlib.reload` lên `_base.process`, và
    reload đúc lại một class `ProcessStateUnknown` MỚI. Nếu orchestrator giữ
    tên đã `import` sẵn thì `except` sẽ trỏ vào class cũ, không khớp nữa, và
    exception lọt ra thành traceback -- chốt chặn im lặng gãy đúng ở chỗ nó
    cần chặn nhất. Reload ngay tại đây để hợp đồng này tự đứng được, không
    phụ thuộc thứ tự chạy của file khác."""
    importlib.reload(bp)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)
    applied: list = []

    _run_apply(prefs_root, tmp_path, applied)

    assert applied == []
    _assert_profile_untouched(prefs_root)
    assert "pgrep" in capsys.readouterr().err


def test_restore_refuses_to_guess_when_running_state_is_unknown(
    prefs_root, tmp_path, monkeypatch
) -> None:
    """`apply --undo` đi qua cùng `running_fn`, và copy backup đè Preferences
    dưới một Brave đang chạy cũng bị flush mất y hệt. Nó phải hỏng to chứ
    không lặng lẽ "restore" rồi mất."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(bp.subprocess, "check_output", _tool_missing)
    profile = prefs_root / "Default"
    backup = profile / "Preferences.bak.20260101-000000"
    backup.write_text(json.dumps({"restored": True}))
    proc = _proc()

    args = argparse.Namespace(
        profile_root=prefs_root,
        profile="Default",
        from_path=None,
        list=False,
        dry_run=False,
        unattended=False,
    )
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.cmd_restore(
            args,
            display_name="Brave",
            running_fn=proc.running,
            find_cmdline_fn=proc.find_main_cmdline,
            restart_fn=lambda c: c,
            graceful_close_fn=lambda: pytest.fail("must not close Brave"),
        )

    assert "pgrep" in str(excinfo.value)
    assert json.loads((profile / "Preferences").read_text()) == _UNTOUCHED
