"""Live apply support for a running Brave instance."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from dotbrave._base.cdp import CdpClient
from dotbrave._base import live_apply as _live
from dotbrave._base.utils import Plan
from dotbrave import shortcuts as shortcuts_mod


_SETTINGS_URL = "chrome://settings/appearance"
_SHORTCUTS_URL = "chrome://settings/system/shortcuts"
_NEWTAB_URL = "chrome://newtab/"
_NEWTAB_ACTIONS = {
    "ntp.shortcust_visible": ("topSites", "setShowTopSites"),
    "brave.brave_search.show-ntp-search": ("search", "setShowSearchBox"),
    "brave.brave_search.show-ntp-chat": ("search", "setShowChatInput"),
    "brave.new_tab_page.show_background_image": (
        "background",
        "setBackgroundsEnabled",
    ),
    "brave.new_tab_page.show_branded_background_image": (
        "background",
        "setSponsoredImagesEnabled",
    ),
    "brave.new_tab_page.show_clock": ("newTab", "setShowClock"),
    "brave.new_tab_page.show_stats": ("newTab", "setShowShieldsStats"),
    "brave.new_tab_page.show_rewards": ("rewards", "setShowRewardsWidget"),
    "brave.new_tab_page.show_brave_vpn": ("vpn", "setShowVpnWidget"),
    "brave.new_tab_page.show_together": ("newTab", "setShowTalkWidget"),
}


def _page_target(client: CdpClient) -> dict:
    for target in client.list_targets():
        if target.get("type") == "page":
            return target
    sys.exit("error: live apply found no page target to drive Brave")


def _worker_target(client: CdpClient) -> tuple[dict, bool]:
    """A page target for driving privileged UI pages.

    Prefer a dedicated new tab so no user tab gets navigated away (the
    caller closes it afterwards); fall back to reusing an existing page
    on endpoints that refuse /json/new -- that one is never closed.
    """
    try:
        return client.create_page("about:blank"), True
    except RuntimeError:
        return _page_target(client), False


def _is_shortcut_path(parts: tuple[str, ...]) -> bool:
    return parts[:2] in {
        shortcuts_mod.ACCELERATORS_KEY_PATH,
        shortcuts_mod.DEFAULT_ACCELERATORS_KEY_PATH,
    }


def _setting_changes(before: dict, target: dict) -> list[tuple[str, Any]]:
    changes = [
        (parts, value)
        for parts, value in _live.changed_leaf_paths(before, target)
        if not _is_shortcut_path(parts)
    ]
    _live.refuse_live_removals("Brave", changes)
    return [(".".join(parts), value) for parts, value in changes]


def _dict_at(prefs: dict, parts: tuple[str, ...]) -> dict[str, list[str]]:
    value = _live.get_path(prefs, parts)
    return value if isinstance(value, dict) else {}


def _shortcut_script(before: dict, target: dict) -> str | None:
    current = _dict_at(before, shortcuts_mod.ACCELERATORS_KEY_PATH)
    defaults = _dict_at(before, shortcuts_mod.DEFAULT_ACCELERATORS_KEY_PATH)
    desired = _dict_at(target, shortcuts_mod.ACCELERATORS_KEY_PATH)
    desired_changes: dict[str, list[str]] = {}
    all_ids = set(current) | set(desired)
    for cid in sorted(all_ids, key=lambda v: int(v)):
        old_keys = list(current[cid] if cid in current else defaults.get(cid, []))
        new_keys = list(desired.get(cid, []))
        if old_keys == new_keys:
            continue
        desired_changes[cid] = new_keys
    if not desired_changes:
        return None
    desired_json = json.dumps(desired_changes, separators=(",", ":"))
    return (
        "(async () => {"
        "const m = await import('/commands.bundle.js');"
        f"const desiredByCommand = {desired_json};"
        "const commandCache = m.commandsCache.cache || {};"
        "for (const [cidText, desired] of Object.entries(desiredByCommand)) {"
        "const cid = Number(cidText);"
        "const command = commandCache[cidText] || commandCache[cid];"
        "const current = (command?.accelerators || [])"
        ".map(a => a.codes || a.keys).filter(Boolean);"
        "for (const key of current) {"
        "if (!desired.includes(key)) "
        "m.commandsCache.unassignAccelerator(cid, key);"
        "}"
        "for (const key of desired) {"
        "if (!current.includes(key)) "
        "m.commandsCache.assignAccelerator(cid, {codes:key, keys:key});"
        "}"
        "}"
        "await new Promise(r => setTimeout(r, 300));"
        "return true;"
        "})()"
    )


def _settings_script(changes: list[tuple[str, Any]]) -> str | None:
    if not changes:
        return None
    calls = "\n".join(
        f"await setPref({json.dumps(key)}, {json.dumps(value)});"
        for key, value in changes
    )
    return (
        "(async () => {"
        "const setPref = (key, value) => new Promise((resolve, reject) => {"
        "chrome.settingsPrivate.setPref(key, value, '', ok => {"
        "const err = chrome.runtime.lastError;"
        "if (err) reject(new Error(err.message));"
        "else if (ok === false) reject(new Error('setPref returned false for ' + key));"
        "else resolve(ok);"
        "});"
        "});"
        f"{calls}"
        "return true;"
        "})()"
    )


def _route_settings(
    changes: list[tuple[str, Any]],
) -> tuple[list[tuple[str, str, str, Any]], list[tuple[str, Any]]]:
    newtab: list[tuple[str, str, str, Any]] = []
    ordinary: list[tuple[str, Any]] = []
    for key, value in changes:
        route = _NEWTAB_ACTIONS.get(key)
        if route is None:
            ordinary.append((key, value))
            continue
        store, action = route
        newtab.append((key, store, action, value))
    return newtab, ordinary


def _newtab_preflight_script(changes: list[tuple[str, str, str, Any]]) -> str | None:
    if not changes:
        return None
    routes = [
        {"key": key, "store": store, "action": action}
        for key, store, action, _value in changes
    ]
    routes_json = json.dumps(routes, separators=(",", ":"))
    return (
        "(async () => {"
        f"const routes = {routes_json};"
        "const missing = () => routes.filter(({store, action}) => "
        "typeof window._ntp?.[store]?.getState?.()?.actions?.[action] "
        "!== 'function').map(({key}) => key);"
        "let unsupported = missing();"
        "for (let attempt = 0; attempt < 20 && unsupported.length; attempt++) {"
        "await new Promise(r => setTimeout(r, 50));"
        "unsupported = missing();"
        "}"
        "return unsupported;"
        "})()"
    )


def _settings_preflight_script(changes: list[tuple[str, Any]]) -> str | None:
    if not changes:
        return None
    keys_json = json.dumps([key for key, _value in changes], separators=(",", ":"))
    return (
        "(async () => {"
        f"const keys = {keys_json};"
        "const exists = key => new Promise(resolve => {"
        "chrome.settingsPrivate.getPref(key, pref => {"
        "const err = chrome.runtime.lastError;"
        "resolve(!err && !!pref);"
        "});"
        "});"
        "const unsupported = [];"
        "for (const key of keys) {"
        "if (!(await exists(key))) unsupported.push(key);"
        "}"
        "return unsupported;"
        "})()"
    )


def _newtab_script(changes: list[tuple[str, str, str, Any]]) -> str | None:
    if not changes:
        return None
    calls = "".join(
        f"window._ntp.{store}.getState().actions.{action}({json.dumps(value)});"
        for _key, store, action, value in changes
    )
    return (
        "(async () => {"
        f"{calls}"
        "await new Promise(r => setTimeout(r, 300));"
        "return true;"
        "})()"
    )


def _preflight_settings(
    client: CdpClient,
    target: dict,
    newtab_changes: list[tuple[str, str, str, Any]],
    ordinary_changes: list[tuple[str, Any]],
) -> list[str]:
    unsupported: list[str] = []
    newtab_script = _newtab_preflight_script(newtab_changes)
    if newtab_script is not None:
        client.navigate(target, _NEWTAB_URL)
        time.sleep(0.5)
        result = client.evaluate(target, newtab_script)
        if isinstance(result, list):
            unsupported.extend(key for key in result if isinstance(key, str))

    settings_script = _settings_preflight_script(ordinary_changes)
    if settings_script is not None:
        client.navigate(target, _SETTINGS_URL)
        time.sleep(0.5)
        result = client.evaluate(target, settings_script)
        if isinstance(result, list):
            unsupported.extend(key for key in result if isinstance(key, str))
    return unsupported


def apply_live(port: int, prefs_path: Path, prefs: dict, plans: list[Plan]) -> None:
    target_prefs = _live.compute_target_prefs(prefs, plans)
    changes = _setting_changes(prefs, target_prefs)
    newtab_changes, ordinary_changes = _route_settings(changes)
    client = CdpClient(port)
    target, created = _worker_target(client)
    try:
        unsupported = _preflight_settings(
            client, target, newtab_changes, ordinary_changes
        )
        if unsupported:
            raise _live.LiveApplyUnsupported("Brave", unsupported)

        has_pref_changes = any(
            not plan.empty and plan.namespace in {"settings", "shortcuts"}
            for plan in plans
        )
        if has_pref_changes:
            _live.backup_preferences(prefs_path)

        _live.apply_external_plans(plans)

        newtab_script = _newtab_script(newtab_changes)
        if newtab_script is not None:
            client.navigate(target, _NEWTAB_URL)
            time.sleep(0.5)
            client.evaluate(target, newtab_script)

        settings_script = _settings_script(ordinary_changes)
        if settings_script is not None:
            client.navigate(target, _SETTINGS_URL)
            time.sleep(0.5)
            client.evaluate(target, settings_script)

        shortcut_script = _shortcut_script(prefs, target_prefs)
        if shortcut_script is not None:
            client.navigate(target, _SHORTCUTS_URL)
            time.sleep(0.5)
            client.evaluate(target, shortcut_script)

        _live.write_state_files(plans)
    finally:
        if created:
            client.close_page(target)
    print("ok -- live applied through Brave DevTools endpoint")
