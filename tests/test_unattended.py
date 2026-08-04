"""`apply --unattended`: không prompt, không đóng Brave, luôn exit 0.

Chế độ này tồn tại cho home-manager activation. Ba lối thoát được kiểm
riêng vì mỗi lối là một câu lệnh khác nhau trong cmd_apply.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave._base import orchestrator
from dotbrave._base.utils import Plan


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
        unattended=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _plan(namespace: str, *, empty: bool, external: bool) -> Plan:
    return Plan(
        namespace=namespace,
        diff_lines=[] if empty else [f"  {namespace}: change"],
        apply_fn=lambda prefs: None,
        verify_fn=lambda prefs: None,
        external_apply_fn=(lambda: None) if external else None,
    )


def test_unattended_skips_privileged_plan_without_sudo(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """[pwa] cần root: bỏ qua, báo ra stderr, không gọi sudo, exit 0."""
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: False)
    called = []
    monkeypatch.setattr(
        orchestrator.subprocess, "run",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("subprocess.run must not be called")
        ),
    )
    cfg = tmp_path / "b.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: False,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("pwa", empty=False, external=True)
        ],
    )

    err = capsys.readouterr().err
    assert "unattended" in err
    assert "[pwa]" in err
    assert called == []


def test_unattended_still_applies_unprivileged_plans(
    prefs_root, tmp_path, capsys
):
    """[pwa] bị bỏ không được kéo theo [settings]."""
    applied = []
    settings = _plan("settings", empty=False, external=False)
    settings.apply_fn = lambda prefs: applied.append("settings")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n[pwa]\nurls = []\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: False,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("pwa", empty=False, external=True),
            settings,
        ],
    )

    assert applied == ["settings"]


def test_unattended_does_not_close_running_browser(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Brave đang chạy, không có live endpoint: bỏ qua chứ không đóng."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    closed = []
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("settings", empty=False, external=False)
        ],
        live_apply_fn=lambda *a: None,
        graceful_close_fn=lambda: closed.append(True),
        launch_live_fn=lambda *a: [],
    )

    assert closed == []
    assert "unattended" in capsys.readouterr().err


def test_unattended_does_not_close_when_live_apply_is_unsupported(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """live_apply_fn từ chối [settings]: bỏ qua chứ không đóng Brave để
    áp offline. Cần một live endpoint THẬT (khác None) để đường đi vào
    được tới live_apply_fn -- test trên chỉ phủ nhánh "chưa có endpoint"."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9555)
    closed = []
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    def _unsupported_live_apply(*_args):
        raise orchestrator.LiveApplyUnsupported("Brave", ["a.b"])

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("settings", empty=False, external=False)
        ],
        live_apply_fn=_unsupported_live_apply,
        graceful_close_fn=lambda: closed.append(True),
        launch_live_fn=lambda *a: [],
    )

    err = capsys.readouterr().err
    assert closed == []
    assert "unattended" in err
    assert "a.b" in err


def test_without_unattended_behaviour_is_unchanged(
    prefs_root, tmp_path, monkeypatch
):
    """Không hồi quy: bỏ cờ thì vẫn đóng Brave như cũ."""
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orchestrator, "pick_unused_port", lambda: 9222)
    monkeypatch.setattr(
        orchestrator, "wait_for_devtools_endpoint", lambda p, n: None
    )
    monkeypatch.setattr(
        orchestrator, "remember_devtools_port", lambda r, p, port: None
    )
    closed = []
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg, unattended=False),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("settings", empty=False, external=False)
        ],
        live_apply_fn=lambda *a: None,
        graceful_close_fn=lambda: closed.append(True),
        launch_live_fn=lambda *a: ["brave"],
    )

    assert closed == [True]


def test_unattended_applies_privileged_plan_when_already_root(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Đã là root/admin: --unattended phải ÁP [pwa], không phải bỏ qua.

    Đây là đường của apply.ps1 trên Windows -- nó chạy sẵn quyền
    Administrator, nên bỏ qua [pwa] là bỏ đúng thứ duy nhất nó cần làm.
    """
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(
        orchestrator.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not shell out when already privileged")
        ),
    )
    applied = []
    pwa = _plan("pwa", empty=False, external=True)
    pwa.external_apply_fn = lambda: applied.append("pwa")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: False,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [pwa],
    )

    assert applied == ["pwa"]
    assert "skipping" not in capsys.readouterr().err


def test_attended_privileged_apply_skips_the_sudo_preflight(
    prefs_root, tmp_path, monkeypatch
):
    """Đã là root thì gọi sudo là thừa, và chết trong môi trường không tương tác."""
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(
        orchestrator.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not call sudo when euid is already 0")
        ),
    )
    applied = []
    pwa = _plan("pwa", empty=False, external=True)
    pwa.external_apply_fn = lambda: applied.append("pwa")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg, unattended=False),
        display_name="Brave",
        running_fn=lambda: False,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [pwa],
    )

    assert applied == ["pwa"]


def test_unattended_applies_pwa_even_when_another_table_is_dirty(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """[pwa] không cần trình duyệt, nên một [shortcuts] bẩn không được chặn nó.

    Đây là trạng thái thật của máy Windows: `apply.ps1` chạy sẵn quyền
    Administrator, Brave đang mở và không có live endpoint, [shortcuts]
    bẩn vì lần cuối áp được là khi Brave đóng. Trước bản sửa này,
    `all(external)` sai nên luồng rơi xuống nhánh live rồi return, và
    [pwa] không bao giờ được ghi -- im lặng, exit 0.
    """
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    calls = []
    shortcuts = _plan("shortcuts", empty=False, external=False)
    shortcuts.apply_fn = lambda prefs: calls.append("shortcuts")
    pwa = _plan("pwa", empty=False, external=True)
    pwa.external_apply_fn = lambda: calls.append("pwa")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[shortcuts]\n"Ctrl+J" = "focusToolbar"\n[pwa]\nurls = []\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [shortcuts, pwa],
        live_apply_fn=lambda *a: calls.append("live"),
        graceful_close_fn=lambda: calls.append("close"),
        launch_live_fn=lambda *a: calls.append("launch") or [],
    )

    # [pwa] applied; nothing touched the running browser.
    assert calls == ["pwa"]


def test_unattended_partial_apply_names_what_landed_and_what_did_not(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Nửa vời phải đọc ra là nửa vời.

    Một dòng "skipping" trơ trọi không cho biết [pwa] ĐÃ ghi; người vận
    hành phải phân biệt được "áp hết" với "áp policy, bỏ phần còn lại".
    """
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    cfg = tmp_path / "b.toml"
    cfg.write_text('[shortcuts]\n"Ctrl+J" = "focusToolbar"\n[pwa]\nurls = []\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("shortcuts", empty=False, external=False),
            _plan("pwa", empty=False, external=True),
        ],
        live_apply_fn=lambda *a: None,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a: [],
    )

    captured = capsys.readouterr()
    assert "[pwa] policy written without touching the running Brave" in captured.out
    assert "[pwa] applied" in captured.err
    assert "[shortcuts] not applied" in captured.err


def test_unattended_applies_pwa_when_live_apply_refuses_the_rest(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Cùng một lỗi ở lối thoát thứ hai: live endpoint có, nhưng từ chối
    [settings]. [pwa] vẫn phải được ghi."""
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: 9555)
    calls = []
    pwa = _plan("pwa", empty=False, external=True)
    pwa.external_apply_fn = lambda: calls.append("pwa")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[settings]\n"a.b" = true\n[pwa]\nurls = []\n')

    def _unsupported_live_apply(*_args):
        raise orchestrator.LiveApplyUnsupported("Brave", ["a.b"])

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            _plan("settings", empty=False, external=False),
            pwa,
        ],
        live_apply_fn=_unsupported_live_apply,
        graceful_close_fn=lambda: calls.append("close"),
        launch_live_fn=lambda *a: [],
    )

    err = capsys.readouterr().err
    assert calls == ["pwa"]
    assert "[pwa] applied" in err
    assert "[settings] not applied" in err
    assert "a.b" in err


def test_all_external_apply_is_unchanged_and_applies_once(
    prefs_root, tmp_path, monkeypatch, capsys
):
    """Không hồi quy ở lối tắt cũ: [pwa] một mình vẫn áp đúng một lần,
    vẫn in nguyên câu cũ, và không đụng tới trình duyệt đang chạy."""
    monkeypatch.setattr(orchestrator, "_already_privileged", lambda: True)
    monkeypatch.setattr(orchestrator, "find_devtools_port", lambda r, p: None)
    calls = []
    pwa = _plan("pwa", empty=False, external=True)
    pwa.external_apply_fn = lambda: calls.append("pwa")
    cfg = tmp_path / "b.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')

    orchestrator.cmd_apply(
        _args(prefs_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [pwa],
        live_apply_fn=lambda *a: calls.append("live"),
        graceful_close_fn=lambda: calls.append("close"),
        launch_live_fn=lambda *a: calls.append("launch") or [],
    )

    captured = capsys.readouterr()
    assert calls == ["pwa"]
    assert (
        "ok -- [pwa] policy written without touching the running Brave "
        "(loaded at its next launch)" in captured.out
    )
    # The whole story: nothing was left undone, so nothing is reported as such.
    assert "not applied" not in captured.err


def test_already_privileged_reads_euid_not_sudo_cache(monkeypatch):
    """`sudo -n true` thành công nghĩa là credential còn cache, KHÁC với đang là root.

    Chỉ cái sau mới cho ghi thẳng, nên helper phải đọc euid.
    """
    monkeypatch.setattr(orchestrator.sys, "platform", "linux")
    # raising=False: os.geteuid doesn't exist on Windows, where this suite
    # also runs (.github/workflows/ci.yml has a windows-latest job). The
    # sys.platform patch above already forces the code under test past its
    # win32 check, so the attribute is safe to fabricate here too.
    monkeypatch.setattr(orchestrator.os, "geteuid", lambda: 0, raising=False)
    assert orchestrator._already_privileged() is True
    monkeypatch.setattr(orchestrator.os, "geteuid", lambda: 501, raising=False)
    assert orchestrator._already_privileged() is False
