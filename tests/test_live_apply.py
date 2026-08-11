from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from dotbrave._base import orchestrator as orch
from dotbrave._base import live_apply
from dotbrave._base.utils import Plan


def _args(profile_root: Path, config: Path, **overrides) -> argparse.Namespace:
    values = {
        "profile_root": profile_root,
        "profile": "Default",
        "config": str(config),
        "dry_run": False,
        "allow_http": False,
        "expect_sha256": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"foo": {"bar": 0}}))
    return tmp_path


def _build_plan(prefs_path: Path, _prefs: dict, _doc: dict, **_kw) -> list[Plan]:
    def apply_fn(prefs: dict) -> None:
        prefs["foo"]["bar"] = 1

    return [
        Plan(
            namespace="settings",
            diff_lines=["  ~ foo.bar: 0 -> 1"],
            apply_fn=apply_fn,
            verify_fn=lambda _prefs: None,
            state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
            state_payload={"managed_keys": ["foo.bar"]},
        )
    ]


def test_running_browser_without_endpoint_gracefully_relaunches_for_live_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    prefs_path = profile_root / "Default" / "Preferences"
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: None)
    monkeypatch.setattr(orch, "pick_unused_port", lambda: 9444)
    monkeypatch.setattr(
        orch,
        "wait_for_devtools_endpoint",
        lambda port, display_name: calls.append(("wait", port)),
    )
    monkeypatch.setattr(
        orch,
        "remember_devtools_port",
        lambda root, profile, port: calls.append(
            ("remember", (root, profile, port))
        ),
    )

    def live_apply_fn(port: int, got_prefs_path: Path, _prefs: dict, plans: list[Plan]) -> None:
        calls.append(("live", (port, got_prefs_path, [p.namespace for p in plans])))

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
    live_apply_fn=live_apply_fn,
        graceful_close_fn=lambda: calls.append(("close", None)),
        launch_live_fn=lambda root, profile, port, url, captured=None: calls.append(
            ("launch", (root, profile, port, url, captured))
        ) or ["brave"],
    )

    assert calls == [
        ("close", None),
        # The captured command line rides along so the relaunch keeps the
        # flags the closed session was started with.
        ("launch", (profile_root, "Default", 9444, None, ["brave"])),
        ("wait", 9444),
        ("live", (9444, prefs_path, ["settings"])),
        ("remember", (profile_root, "Default", 9444)),
    ]


def test_running_browser_reuses_existing_devtools_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    calls: list[str] = []

    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: 9555)

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=lambda port, *_args: calls.append(f"live:{port}"),
        graceful_close_fn=lambda: calls.append("close"),
        launch_live_fn=lambda *_args: calls.append("launch") or ["brave"],
    )

    assert calls == ["live:9555"]


def test_plain_live_apply_unsupported_setting_falls_back_without_force_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    prefs_path = profile_root / "Default" / "Preferences"
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: 9555)
    monkeypatch.setattr(
        orch,
        "wait_for_devtools_endpoint",
        lambda port, display_name: calls.append(("wait", (port, display_name))),
    )
    monkeypatch.setattr(
        orch,
        "remember_devtools_port",
        lambda root, profile, port: calls.append(
            ("remember", (root, profile, port))
        ),
    )

    def unsupported_live_apply(*_args: object) -> None:
        raise live_apply.LiveApplyUnsupported("Brave", ["foo.bar"])

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=unsupported_live_apply,
        graceful_close_fn=lambda: calls.append(("close", None)),
        launch_live_fn=lambda root, profile, port, url, captured=None: calls.append(
            ("launch", (root, profile, port, url, captured))
        ) or ["brave"],
    )

    assert json.loads(prefs_path.read_text()) == {"foo": {"bar": 1}}
    assert calls == [
        ("close", None),
        ("launch", (profile_root, "Default", 9555, None, ["brave"])),
        ("wait", (9555, "Brave")),
        ("remember", (profile_root, "Default", 9555)),
    ]


def _sudo_preflight_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        # Windows escalates through IsUserAnAdmin(), not sudo.
        import ctypes

        monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
        return

    import subprocess

    def fake_run(cmd, *args, **kwargs):
        if list(cmd[:2]) in (["sudo", "-n"], ["sudo", "-v"]):
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(orch.subprocess, "run", fake_run)


def _build_pwa_plan(calls: list[tuple[str, object]]):
    def build(_prefs_path: Path, _prefs: dict, _doc: dict, **_kw) -> list[Plan]:
        return [
            Plan(
                namespace="pwa",
                diff_lines=["  + https://example.com"],
                apply_fn=lambda _prefs: None,
                verify_fn=lambda _prefs: None,
                external_apply_fn=lambda: calls.append(("external", None)),
            )
        ]

    return build


def test_pwa_only_apply_leaves_running_browser_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')
    prefs_path = profile_root / "Default" / "Preferences"
    calls: list[tuple[str, object]] = []

    _sudo_preflight_ok(monkeypatch)
    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: None)

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_pwa_plan(calls),
        live_apply_fn=lambda *_a: calls.append(("live", None)),
        graceful_close_fn=lambda: calls.append(("close", None)),
        launch_live_fn=lambda *_a: calls.append(("launch", None)) or ["brave"],
    )

    assert calls == [("external", None)]
    # Preferences and its directory stay untouched: no write, no backup.
    assert json.loads(prefs_path.read_text()) == {"foo": {"bar": 0}}
    assert not list(prefs_path.parent.glob("Preferences.bak.*"))
    out = capsys.readouterr().out
    assert "without touching the running Brave" in out


def test_pwa_only_apply_without_live_adapter_skips_close_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')
    calls: list[tuple[str, object]] = []

    _sudo_preflight_ok(monkeypatch)

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Chrome",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["chrome"],
        restart_fn=lambda cmd: calls.append(("restart", cmd)) or cmd,
        build_plans_fn=_build_pwa_plan(calls),
        graceful_close_fn=lambda: calls.append(("close", None)),
    )

    assert calls == [("external", None)]


def test_pwa_with_settings_changes_still_bootstraps_live_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[settings]\nfoo.bar = 1\n[pwa]\nurls = ["https://example.com"]\n')
    calls: list[tuple[str, object]] = []

    _sudo_preflight_ok(monkeypatch)
    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: None)
    monkeypatch.setattr(orch, "pick_unused_port", lambda: 9444)
    monkeypatch.setattr(
        orch, "wait_for_devtools_endpoint", lambda port, name: calls.append(("wait", port))
    )
    monkeypatch.setattr(
        orch, "remember_devtools_port", lambda root, profile, port: calls.append(("remember", port))
    )

    def build(prefs_path: Path, _prefs: dict, _doc: dict, **_kw) -> list[Plan]:
        def apply_fn(prefs: dict) -> None:
            prefs["foo"]["bar"] = 1

        return [
            Plan(
                namespace="settings",
                diff_lines=["  ~ foo.bar: 0 -> 1"],
                apply_fn=apply_fn,
                verify_fn=lambda _prefs: None,
                state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
                state_payload={"managed_keys": ["foo.bar"]},
            ),
            Plan(
                namespace="pwa",
                diff_lines=["  + https://example.com"],
                apply_fn=lambda _prefs: None,
                verify_fn=lambda _prefs: None,
                external_apply_fn=lambda: calls.append(("external", None)),
            ),
        ]

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=build,
        live_apply_fn=lambda port, _path, _prefs, plans: calls.append(
            ("live", (port, [p.namespace for p in plans]))
        ),
        graceful_close_fn=lambda: calls.append(("close", None)),
        launch_live_fn=lambda root, profile, port, url, captured=None: calls.append(
            ("launch", port)
        ) or ["brave"],
    )

    # The managed policy needs no browser, so it is written before any of
    # the close/relaunch dance -- and it must NOT reach the live adapter,
    # which runs external_apply_fn itself and would write it twice.
    assert calls == [
        ("external", None),
        ("close", None),
        ("launch", 9444),
        ("wait", 9444),
        ("live", (9444, ["settings"])),
        ("remember", 9444),
    ]


def test_running_browser_without_live_adapter_closes_normally_and_restarts(
    tmp_path: Path,
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    prefs_path = profile_root / "Default" / "Preferences"
    calls: list[tuple[str, object]] = []

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Chrome",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["chrome"],
        restart_fn=lambda cmd: calls.append(("restart", cmd)) or cmd,
        build_plans_fn=_build_plan,
        graceful_close_fn=lambda: calls.append(("close", None)),
    )

    assert json.loads(prefs_path.read_text()) == {"foo": {"bar": 1}}
    assert calls == [("close", None), ("restart", ["chrome"])]


def test_offline_fallback_does_not_discard_what_the_browser_flushed_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing Brave flushes its own Preferences copy; the offline write
    must build on that file, not on the snapshot read before the close."""
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    prefs_path = profile_root / "Default" / "Preferences"

    def flush_on_close() -> None:
        # What Brave writes as it exits: a key dotbrave never saw.
        data = json.loads(prefs_path.read_text())
        data["profile"] = {"exit_type": "Normal"}
        prefs_path.write_text(json.dumps(data))

    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: 9444)

    def live_apply_fn(port, got_prefs_path, _prefs, plans):
        raise live_apply.LiveApplyUnsupported("Brave", ["foo.bar"])

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=live_apply_fn,
        graceful_close_fn=flush_on_close,
        launch_live_fn=lambda root, profile, port, url, captured=None: ["brave"],
    )

    final = json.loads(prefs_path.read_text())
    assert final["profile"]["exit_type"] == "Normal", "close-time flush was reverted"
    assert final["foo"]["bar"] == 1, "the requested change was not applied"


def test_live_setting_removal_signals_offline_fallback() -> None:
    with pytest.raises(live_apply.LiveApplyUnsupported) as exc:
        live_apply.refuse_live_removals(
            "Chrome", [(("foo", "bar"), live_apply.MISSING)]
        )

    assert exc.value.keys == ["foo.bar"]


def test_changed_leaf_paths_expands_new_nested_dicts() -> None:
    before = {"brave": {}}
    after = {"brave": {"tabs": {"vertical_tabs_enabled": True}}}

    assert live_apply.changed_leaf_paths(before, after) == [
        (("brave", "tabs", "vertical_tabs_enabled"), True)
    ]
