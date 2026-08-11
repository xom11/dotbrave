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
        self.created: list[dict] = []
        self.closed: list[dict] = []
        self.refuse_create = False

    def list_targets(self) -> list[dict]:
        return self.targets

    def create_page(self, url: str = "about:blank") -> dict:
        if self.refuse_create:
            raise RuntimeError("endpoint refuses /json/new")
        target = {"type": "page", "url": url, "id": f"work-{len(self.created)}"}
        self.created.append(target)
        self.targets.append(target)
        return target

    def close_page(self, target: dict) -> None:
        self.closed.append(target)

    def navigate(self, target: dict, url: str) -> None:
        self.navigations.append(url)
        target["url"] = url

    def evaluate(self, target: dict, expression: str):
        self.evaluations.append(expression)
        if "readyState" in expression:
            # Recognize the page-readiness probe by its expression text
            # and answer it directly, rather than consuming a scripted
            # result meant for the real preflight/mutation call that
            # follows -- production polls this in a loop, and tests
            # should not have to script a value for every poll.
            return True
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


def test_brave_live_preflight_names_unknown_settings_as_the_remainder(
    tmp_path: Path, monkeypatch
) -> None:
    """When the unknown key is the *whole* diff the remainder is the whole
    diff too: nothing is pushed through setPref, nothing is backed up, and
    the key is named so the offline path can report it."""
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

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert excinfo.value.keys == ["brave.tabs.vertical_tabs_collapsed"]
    assert any("chrome.settingsPrivate.getPref" in expr for expr in fake.evaluations)
    assert not any("chrome.settingsPrivate.setPref" in expr for expr in fake.evaluations)
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []


def _split_prefs(prefs_path: Path) -> tuple[dict, str]:
    """A profile whose apply touches one unsupported key, one supported
    key and one shortcut -- the three halves a split run has to separate."""
    from dotbrave.command_ids import NAME_TO_ID

    new_tab = str(NAME_TO_ID["new_tab"])
    prefs = {
        "brave": {
            "tabs": {"vertical_tabs_collapsed": False},
            "location_bar_is_wide": False,
            "accelerators": {new_tab: ["Control+KeyT"]},
            "default_accelerators": {new_tab: ["Control+KeyT"]},
        }
    }
    prefs_path.write_text(json.dumps(prefs))
    return prefs, new_tab


def _split_plan(prefs_path: Path, new_tab: str) -> Plan:
    def apply_fn(target: dict) -> None:
        target["brave"]["tabs"]["vertical_tabs_collapsed"] = True  # unsupported
        target["brave"]["location_bar_is_wide"] = True  # supported
        target["brave"]["accelerators"][new_tab] = ["Control+Shift+KeyY"]

    return Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
        state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
        state_payload={"managed_keys": ["brave.location_bar_is_wide"]},
    )


def test_one_unsupported_key_no_longer_drags_the_whole_run_offline(
    tmp_path: Path, monkeypatch
) -> None:
    """A key settingsPrivate does not know is the *only* thing that goes
    offline: the supported key and the independent [shortcuts] table are
    applied live first."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs, new_tab = _split_prefs(prefs_path)
    plan = _split_plan(prefs_path, new_tab)

    # settings preflight reports only the vertical-tabs key as unsupported;
    # the shortcuts preflight that follows returns the default [].
    fake = FakeCdpClient(
        9333, evaluation_results=[["brave.tabs.vertical_tabs_collapsed"]]
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    # the remainder is named, and only the remainder
    assert excinfo.value.keys == ["brave.tabs.vertical_tabs_collapsed"]
    # ...but the supported key and the shortcut were applied live first
    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
    assert any("assignAccelerator" in e for e in fake.evaluations)
    # and the unsupported key was never pushed through settingsPrivate
    assert not any(
        "vertical_tabs_collapsed" in e and "setPref" in e
        for e in fake.evaluations
    )


def test_split_run_takes_no_live_backup(tmp_path: Path, monkeypatch) -> None:
    """The orchestrator backs up for the offline remainder; a second
    backup here would violate invariant 1.  State files stay unwritten
    for the same reason: the offline apply writes them for every plan."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs, new_tab = _split_prefs(prefs_path)
    plan = _split_plan(prefs_path, new_tab)

    fake = FakeCdpClient(
        9333, evaluation_results=[["brave.tabs.vertical_tabs_collapsed"]]
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan])

    # This is a split run, not an early refusal: the live half really ran.
    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
    backups = list(prefs_path.parent.glob("Preferences.bak.*"))
    assert backups == [], f"live path took a backup during a split run: {backups}"
    assert not plan.state_path.exists(), "state file claims a plan not fully applied"


def test_removal_goes_offline_alone_while_the_rest_applies_live(
    tmp_path: Path, monkeypatch
) -> None:
    """settingsPrivate has no single-pref reset, so a dropped key must go
    offline -- but it must take nothing else with it."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {
        "brave": {
            "location_bar_is_wide": False,
            "tabs": {"vertical_tabs_enabled": False},
        }
    }
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["location_bar_is_wide"] = True
        del target["brave"]["tabs"]["vertical_tabs_enabled"]

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
    )
    fake = FakeCdpClient(9333)  # preflight reports nothing unsupported
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert excinfo.value.keys == ["brave.tabs.vertical_tabs_enabled"]
    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []


def test_removal_only_diff_never_opens_a_work_tab(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing in this diff has a live route, so do not open (and close) a
    tab in the user's browser just to refuse."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        del target["brave"]["tabs"]["vertical_tabs_enabled"]

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
    )
    fake = FakeCdpClient(9333)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert excinfo.value.keys == ["brave.tabs.vertical_tabs_enabled"]
    assert fake.created == []
    assert fake.navigations == []


def test_settings_remainder_does_not_block_the_shortcut_script(
    tmp_path: Path, monkeypatch
) -> None:
    """Ordering guard: the shortcut script must run *before* the raise."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs, new_tab = _split_prefs(prefs_path)
    plan = _split_plan(prefs_path, new_tab)

    fake = FakeCdpClient(
        9333, evaluation_results=[["brave.tabs.vertical_tabs_collapsed"]]
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert "chrome://settings/system/shortcuts" in fake.navigations
    assert any("commandsCache.assignAccelerator" in e for e in fake.evaluations)


def test_broken_shortcuts_bundle_does_not_block_live_settings(
    tmp_path: Path, monkeypatch
) -> None:
    """The mirror case: shortcuts go offline, the settings still go live."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs, new_tab = _split_prefs(prefs_path)
    plan = _split_plan(prefs_path, new_tab)

    # settings preflight: everything supported; shortcuts preflight: broken.
    fake = FakeCdpClient(9333, evaluation_results=[[], ["shortcuts"]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    assert excinfo.value.keys == ["shortcuts"]
    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
    # the bundle reported itself unusable -- do not drive it anyway
    assert not any("commandsCache.assignAccelerator" in e for e in fake.evaluations)


def _settings_plan(prefs_path: Path) -> Plan:
    def apply_fn(target: dict) -> None:
        target["brave"]["tabs"]["vertical_tabs_enabled"] = True

    return Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=apply_fn,
        verify_fn=lambda _prefs: None,
        state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
        state_payload={"managed_keys": ["brave.tabs.vertical_tabs_enabled"]},
    )


def test_live_apply_uses_dedicated_tab_and_closes_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Live apply must not hijack a user tab: it opens its own work tab
    and closes it afterwards, even though it navigates privileged pages."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs))

    fake = FakeCdpClient(9333)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)
    live.apply_live(9333, prefs_path, prefs, [_settings_plan(prefs_path)])

    assert len(fake.created) == 1
    assert fake.closed == fake.created
    # The pre-existing user tab was never navigated.
    assert fake.targets[0]["url"] == "chrome://newtab/"


def test_live_apply_closes_work_tab_when_preflight_rejects(
    tmp_path: Path, monkeypatch
) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs))

    fake = FakeCdpClient(
        9333, evaluation_results=[["brave.tabs.vertical_tabs_enabled"]]
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)
    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [_settings_plan(prefs_path)])
    assert fake.closed == fake.created


def test_live_apply_falls_back_to_existing_tab_without_closing(
    tmp_path: Path, monkeypatch
) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs))

    fake = FakeCdpClient(9333)
    fake.refuse_create = True
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)
    live.apply_live(9333, prefs_path, prefs, [_settings_plan(prefs_path)])

    assert fake.created == []
    assert fake.closed == []  # never close a tab we did not open
    assert "chrome://settings/appearance" in fake.navigations


def test_shortcuts_preflight_reports_unsupported_instead_of_failing_hard(
    tmp_path: Path, monkeypatch
) -> None:
    """A renamed commands bundle must degrade to the offline path."""
    from dotbrave.command_ids import NAME_TO_ID

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    new_tab = str(NAME_TO_ID["new_tab"])
    prefs = {"brave": {"accelerators": {new_tab: ["Control+KeyT"]},
                       "default_accelerators": {new_tab: ["Control+KeyT"]}}}
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["accelerators"][new_tab] = ["Control+Shift+KeyY"]

    plan = Plan(namespace="shortcuts", diff_lines=["changed"],
                apply_fn=apply_fn, verify_fn=lambda _p: None)

    # settings preflight returns [], shortcuts preflight reports itself broken
    fake = FakeCdpClient(9333, evaluation_results=[["shortcuts"]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])
    assert any("shortcut" in k for k in excinfo.value.keys)


def test_scripts_guard_against_an_unloaded_settings_page() -> None:
    """The probe must degrade like the NTP one, not throw on a cold page."""
    script = live._settings_preflight_script([("foo.bar", 1)])
    assert "chrome?.settingsPrivate" in script or "typeof chrome" in script


def test_await_page_polls_until_ready() -> None:
    """`_await_page` must actually poll, not just check once and return.

    ``Page.navigate`` returns as soon as navigation starts, so a client
    that answers "not ready" a few times before "ready" is the case this
    task's fix exists for. An implementation that evaluated once and
    returned (no loop) would call ``evaluate`` exactly once here; this
    pins that it is called more than once.
    """
    calls = {"n": 0}

    class ReadyAfterAFewClient:
        def evaluate(self, target: dict, expression: str):
            calls["n"] += 1
            # Falsy answers on the first couple of polls, then ready.
            return calls["n"] >= 3

    live._await_page(ReadyAfterAFewClient(), {})

    assert calls["n"] > 1


def test_await_page_returns_after_timeout_instead_of_hanging(monkeypatch) -> None:
    """A page that never reports ready must not hang or raise -- the
    bounded wait gives up and lets the guarded scripts that follow
    report themselves unsupported instead."""

    class NeverReadyClient:
        def evaluate(self, target: dict, expression: str):
            return False

    # First call establishes the deadline; the loop's own next check must
    # already read past it, so the wait resolves without a real sleep.
    ticks = iter([0.0, 0.5, 100.0])

    def fake_monotonic() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 100.0

    monkeypatch.setattr(live.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(live.time, "sleep", lambda seconds: None)

    live._await_page(NeverReadyClient(), {}, timeout=1.0)  # must not raise
