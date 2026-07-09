from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotbrave._base import live_apply as shared_live
from dotbrave._base.utils import Plan
from dotbrave import live


class FakeCdpClient:
    def __init__(self, port: int, evaluation_results: list[object] | None = None):
        self.port = port
        self.targets = [{"type": "page", "url": "chrome://newtab/"}]
        self.navigations: list[str] = []
        self.evaluations: list[str] = []
        self.evaluation_results = iter(evaluation_results or [])

    def list_targets(self) -> list[dict]:
        return self.targets

    def navigate(self, target: dict, url: str) -> None:
        self.navigations.append(url)
        target["url"] = url

    def evaluate(self, target: dict, expression: str):
        self.evaluations.append(expression)
        return next(self.evaluation_results, [])


def test_brave_live_apply_uses_settings_private_and_commands_service(
    tmp_path: Path, monkeypatch
) -> None:
    from dotbrave.command_ids import NAME_TO_ID

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    new_tab = str(NAME_TO_ID["new_tab"])
    prefs = {
        "brave": {
            "tabs": {"vertical_tabs_enabled": False},
            "accelerators": {new_tab: ["Control+KeyT"]},
            "default_accelerators": {new_tab: ["Control+KeyT"]},
        }
    }
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["tabs"]["vertical_tabs_enabled"] = True
        target["brave"]["accelerators"][new_tab] = ["Control+Shift+KeyY"]

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
        state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
        state_payload={"managed_keys": ["brave.tabs.vertical_tabs_enabled"]},
    )

    fake = FakeCdpClient(9333)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    live.apply_live(9333, prefs_path, prefs, [plan])

    assert "chrome://settings/system/shortcuts" in fake.navigations
    assert any(
        "chrome.settingsPrivate.setPref" in expr
        and "brave.tabs.vertical_tabs_enabled" in expr
        and "true" in expr
        for expr in fake.evaluations
    )
    assert any("commandsCache.cache" in expr for expr in fake.evaluations)
    assert any("commandsCache.assignAccelerator" in expr for expr in fake.evaluations)
    assert any("commandsCache.unassignAccelerator" in expr for expr in fake.evaluations)
    assert any('"34014":["Control+Shift+KeyY"]' in expr for expr in fake.evaluations)
    state = json.loads(prefs_path.with_name("Preferences.dotbrave.settings.json").read_text())
    assert state["managed_keys"] == ["brave.tabs.vertical_tabs_enabled"]


def test_brave_live_uses_default_accelerator_when_current_binding_is_missing() -> None:
    from dotbrave.command_ids import NAME_TO_ID

    new_tab = str(NAME_TO_ID["new_tab"])
    close_tab = str(NAME_TO_ID["close_tab"])
    before = {
        "brave": {
            "accelerators": {},
            "default_accelerators": {
                new_tab: ["Control+KeyT"],
                close_tab: ["Control+KeyW"],
            },
        }
    }
    target = {
        "brave": {
            "accelerators": {new_tab: ["Control+Shift+KeyY"]},
            "default_accelerators": {new_tab: ["Control+KeyT"]},
        }
    }

    script = live._shortcut_script(before, target)

    assert script is not None
    assert "commandsCache.cache" in script
    assert f'"{new_tab}":["Control+Shift+KeyY"]' in script
    assert f'"{close_tab}"' not in script
    assert "commandsCache.unassignAccelerator" in script
    assert "commandsCache.assignAccelerator" in script


def test_brave_live_routes_new_tab_settings_through_new_tab_actions(
    tmp_path: Path, monkeypatch
) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {
        "ntp": {"shortcust_visible": True},
        "brave": {"brave_search": {"show-ntp-search": True}},
    }
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["ntp"]["shortcust_visible"] = False
        target["brave"]["brave_search"]["show-ntp-search"] = False

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
    )
    fake = FakeCdpClient(9333)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    live.apply_live(9333, prefs_path, prefs, [plan])

    assert "chrome://newtab/" in fake.navigations
    assert any("setShowTopSites(false)" in expr for expr in fake.evaluations)
    assert any("setShowSearchBox(false)" in expr for expr in fake.evaluations)
    assert not any("chrome.settingsPrivate.setPref" in expr for expr in fake.evaluations)


def test_brave_live_preflight_rejects_unknown_settings_before_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"tabs": {"vertical_tabs_collapsed": False}}}
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["tabs"]["vertical_tabs_collapsed"] = True

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
    )
    fake = FakeCdpClient(
        9333, evaluation_results=[["brave.tabs.vertical_tabs_collapsed"]]
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert any("chrome.settingsPrivate.getPref" in expr for expr in fake.evaluations)
    assert not any("chrome.settingsPrivate.setPref" in expr for expr in fake.evaluations)
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []
