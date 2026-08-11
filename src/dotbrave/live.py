"""Live apply support for a running Brave instance."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotbrave._base.cdp import CdpClient, CdpError
from dotbrave._base import live_apply as _live
from dotbrave._base import settings as _base_settings
from dotbrave._base.utils import Plan
from dotbrave import shortcuts as shortcuts_mod


_SETTINGS_URL = "chrome://settings/appearance"
_SHORTCUTS_URL = "chrome://settings/system/shortcuts"
_NEWTAB_URL = "chrome://newtab/"
_VERSION_URL = "chrome://version/"
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
    raise CdpError("live apply found no page target to drive Brave")


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


_READY_EXPR = "document.readyState === 'complete'"


def _await_page(client: CdpClient, target: dict, timeout: float = 10.0) -> None:
    """Poll until the page has a live JS context.

    ``Page.navigate`` returns when navigation *starts*, not once the page
    has loaded; the old fixed 0.5s sleep turned a cold profile or a slow
    machine into a failed live apply. If the deadline passes without the
    page reporting ready, proceed anyway rather than hang the run forever
    -- the guarded scripts that follow report themselves unsupported
    instead of crashing against a half-loaded page, so the run still
    degrades to the offline fallback.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.evaluate(target, _READY_EXPR) is True:
            return
        time.sleep(0.1)


# chrome://version renders the loaded profile's own directory. Upstream
# pins the element id in two places that must agree, so a rename would
# break Chromium's own page:
#
#   components/webui/version/resources/about_version.html
#     <td class="version" id="profile_path">$i18n{profile_path}</td>
#   components/webui/version/resources/about_version.ts
#     getRequiredElement('profile_path').textContent = profilePath;
#
# Brave ships no patch for either file (its only about_version patch is
# the .css one), so the id holds on Brave too.
#
# The value is *not* in the initial markup: about_version.ts fills it from
# a `requestPathInfo` round-trip to the browser process, so readyState
# being 'complete' does not mean it has arrived -- poll for it, the way
# the NTP preflight polls for its store.  Returning '' when it never
# arrives is deliberate: the caller fails closed on an empty answer.
_PROFILE_PATH_SCRIPT = (
    "(async () => {"
    "const read = () => {"
    "const el = document.getElementById('profile_path');"
    "return el ? (el.textContent || '').trim() : '';"
    "};"
    "let value = read();"
    "for (let attempt = 0; attempt < 40 && !value; attempt++) {"
    "await new Promise(r => setTimeout(r, 50));"
    "value = read();"
    "}"
    "return value;"
    "})()"
)


def _same_profile_dir(seen: str, expected: Path) -> bool:
    """Compare two profile directories as paths, not as strings.

    ``chrome://version`` prints ``profile_path.LossyDisplayName()`` -- the
    path Brave was handed, not an absolutised one -- so it can differ from
    ours by a trailing separator or by a symlinked ``--user-data-dir``
    while naming the same directory.  ``resolve()`` settles both; a path
    the OS refuses to resolve is simply not a match (fail closed).
    """
    try:
        a = os.path.normcase(str(Path(seen).resolve()))
        b = os.path.normcase(str(Path(expected).resolve()))
    except (OSError, ValueError, RuntimeError):
        return False
    return a == b


def _confirm_profile(
    client: CdpClient, target: dict, profile_dir: Path, profile: str,
) -> None:
    """Refuse to write through a work tab that is not this run's profile.

    ``PUT /json/new`` carries no profile hint: upstream builds the new
    target's ``NavigateParams`` with ``ProfileManager::GetLastUsedProfile()``
    (chrome/browser/devtools/chrome_devtools_manager_delegate.cc), so the
    work tab lands in the browser's *last-used* profile while everything
    else in the run -- the diff, the backup, the sidecars, ``verify_fn`` --
    is bound to the profile the user asked for.  Endpoint discovery does
    not cover this: only the ``.dotbrave.live.json`` sidecar is
    profile-aware, and both fallbacks (``DevToolsActivePort`` and the
    running command line) are profile-blind.  The reused-page fallback in
    ``_worker_target`` is likelier still to be another profile's tab.

    An unconfirmed profile is treated exactly like a mismatch, and both
    degrade the run to the existing close -> offline apply -> relaunch.
    That trades a visible close/relaunch (should the id ever move) for the
    silent cross-profile write this check exists to remove.
    """
    client.navigate(target, _VERSION_URL)
    _await_page(client, target)
    seen = client.evaluate(target, _PROFILE_PATH_SCRIPT)
    if not isinstance(seen, str) or not seen.strip():
        raise _live.LiveApplyUnsupported(
            "Brave",
            [
                f"cannot confirm the live work tab belongs to profile "
                f"{profile!r} ({profile_dir})"
            ],
        )
    seen = seen.strip()
    if not _same_profile_dir(seen, profile_dir):
        raise _live.LiveApplyUnsupported(
            "Brave",
            [
                f"the live work tab is in {seen}, not profile "
                f"{profile!r} ({profile_dir})"
            ],
        )


def _is_shortcut_path(parts: tuple[str, ...]) -> bool:
    return parts[:2] in {
        shortcuts_mod.ACCELERATORS_KEY_PATH,
        shortcuts_mod.DEFAULT_ACCELERATORS_KEY_PATH,
    }


def _setting_changes(
    before: dict, target: dict
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Split the settings diff into what live apply can push and what it
    cannot.  Removals are returned, not refused: ``settingsPrivate`` has
    no single-pref reset, but that is a fact about those keys alone and
    must not drag the rest of the run offline with them."""
    changes = [
        (parts, value)
        for parts, value in _live.changed_leaf_paths(before, target)
        if not _is_shortcut_path(parts)
    ]
    applicable, removals = _live.split_removals(changes)
    return [(".".join(parts), value) for parts, value in applicable], removals


def _resolve_removals(
    prefs_path: Path, removals: list[str],
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Turn removals into live writes where a prior value was recorded.

    ``settingsPrivate`` cannot delete a pref, but writing back what the
    key held before dotbrave managed it is the same thing as far as the
    browser's behaviour is concerned.  A key recorded ``present: false``
    was never set, so there is no value to write and it has to be deleted
    offline.  The sidecar entry is read defensively: a missing, malformed,
    or partially-written entry (not a dict, missing ``present``, or missing
    ``value``) is treated the same as "no prior value recorded" rather than
    raising.
    """
    if not removals:
        return [], []
    prior = _base_settings.get_prior_values(prefs_path)
    writes: list[tuple[str, Any]] = []
    unresolved: list[str] = []
    for key in removals:
        entry = prior.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("present") is True
            and "value" in entry
        ):
            writes.append((key, entry["value"]))
        else:
            unresolved.append(key)
    return writes, unresolved


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
    keys_json = json.dumps([key for key, _value in changes], separators=(",", ":"))
    calls = "\n".join(
        f"await setPref({json.dumps(key)}, {json.dumps(value)});"
        for key, value in changes
    )
    return (
        "(async () => {"
        f"const keys = {keys_json};"
        "if (typeof chrome === 'undefined' || !chrome.settingsPrivate) "
        "throw new Error("
        "'chrome.settingsPrivate unavailable for ' + keys.join(', '));"
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
        "if (typeof chrome === 'undefined' || !chrome.settingsPrivate) "
        "return keys.slice();"
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


def _shortcuts_preflight_script() -> str:
    """Probe the commands bundle the way the NTP probe works: report
    unusable rather than throwing, so a renamed export degrades to an
    offline apply instead of aborting the run."""
    return (
        "(async () => {"
        "try {"
        "const m = await import('/commands.bundle.js');"
        "const c = m.commandsCache;"
        "if (!c || typeof c.assignAccelerator !== 'function' "
        "|| typeof c.unassignAccelerator !== 'function') return ['shortcuts'];"
        "return [];"
        "} catch (e) { return ['shortcuts']; }"
        "})()"
    )


def _newtab_script(changes: list[tuple[str, str, str, Any]]) -> str | None:
    if not changes:
        return None
    # readyState only reflects resource loading, not SPA store hydration --
    # window._ntp can still be absent right after navigate. Poll the same
    # way _newtab_preflight_script already proves works, on this page's own
    # navigation, rather than trusting the earlier preflight's readiness
    # (that ran against a *different* navigate to the same URL).
    routes = [
        {"key": key, "store": store, "action": action}
        for key, store, action, _value in changes
    ]
    routes_json = json.dumps(routes, separators=(",", ":"))
    calls = "".join(
        f"window._ntp.{store}.getState().actions.{action}({json.dumps(value)});"
        for _key, store, action, value in changes
    )
    return (
        "(async () => {"
        f"const routes = {routes_json};"
        "const missing = () => routes.filter(({store, action}) => "
        "typeof window._ntp?.[store]?.getState?.()?.actions?.[action] "
        "!== 'function').map(({key}) => key);"
        "let unready = missing();"
        "for (let attempt = 0; attempt < 20 && unready.length; attempt++) {"
        "await new Promise(r => setTimeout(r, 50));"
        "unready = missing();"
        "}"
        "if (unready.length) throw new Error("
        "'NTP store actions unavailable for ' + unready.join(', '));"
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
        _await_page(client, target)
        result = client.evaluate(target, newtab_script)
        if isinstance(result, list):
            unsupported.extend(key for key in result if isinstance(key, str))

    settings_script = _settings_preflight_script(ordinary_changes)
    if settings_script is not None:
        client.navigate(target, _SETTINGS_URL)
        _await_page(client, target)
        result = client.evaluate(target, settings_script)
        if isinstance(result, list):
            unsupported.extend(key for key in result if isinstance(key, str))
    return unsupported


def _preflight_shortcuts(client: CdpClient, target: dict) -> list[str]:
    client.navigate(target, _SHORTCUTS_URL)
    _await_page(client, target)
    result = client.evaluate(target, _shortcuts_preflight_script())
    if isinstance(result, list):
        return [key for key in result if isinstance(key, str)]
    return []


def apply_live(
    port: int,
    prefs_path: Path,
    prefs: dict,
    plans: list[Plan],
    *,
    unattended: bool = False,
    profile: str | None = None,
) -> None:
    profile_dir = prefs_path.parent
    profile_name = profile or profile_dir.name
    target_prefs = _live.compute_target_prefs(prefs, plans)
    changes, removals = _setting_changes(prefs, target_prefs)
    newtab_changes, ordinary_changes = _route_settings(changes)
    # Resolve removals against the sidecar's recorded prior values *before*
    # the preflight runs, and fold the resolved writes into the ordinary
    # settings changes so the preflight probes them too -- a prior value
    # for a key settingsPrivate does not recognise is still unusable, and
    # only the preflight can tell us that.
    removal_writes, unresolved_removals = _resolve_removals(prefs_path, removals)
    ordinary_changes = ordinary_changes + removal_writes
    shortcut_script = _shortcut_script(prefs, target_prefs)
    if removals and not (newtab_changes or ordinary_changes or shortcut_script):
        # A diff that is nothing but removals -- none of which had a prior
        # value to restore -- has no live half at all, so refuse before
        # touching the browser rather than opening a work tab in the
        # user's face only to close it again.
        raise _live.LiveApplyUnsupported("Brave", sorted(removals))
    client = CdpClient(port)
    target: dict = {}
    created = False
    backup_taken = False
    try:
        target, created = _worker_target(client)
        # Before the preflight, before the backup, before anything is
        # mutated: prove this tab is the profile the rest of the run is
        # bound to.  Inside the try, so the `finally` still closes it.
        _confirm_profile(client, target, profile_dir, profile_name)
        unsupported = _preflight_settings(
            client, target, newtab_changes, ordinary_changes
        )
        shortcuts_unsupported: list[str] = []
        if shortcut_script is not None:
            shortcuts_unsupported = _preflight_shortcuts(client, target)

        # The split is per key, not per run.  One key settingsPrivate does
        # not know used to send everything offline -- every key that would
        # have worked, plus the whole [shortcuts] table, which has nothing
        # to do with it.  Blocked keys are the remainder and only that.
        # A removal already resolved into `ordinary_changes` above lands in
        # `blocked` via `unsupported` if the preflight rejects it too;
        # `unresolved_removals` covers the ones with no prior value at all.
        blocked = set(unsupported) | set(unresolved_removals)
        live_newtab = [c for c in newtab_changes if c[0] not in blocked]
        live_ordinary = [c for c in ordinary_changes if c[0] not in blocked]
        remainder = sorted(blocked | set(shortcuts_unsupported))

        if remainder and unattended:
            # Unattended keeps the old all-or-nothing semantics, and must
            # refuse *before* mutating anything.  Nothing closes the
            # browser for the remainder in this mode, so a live half
            # applied here would never reach write_state_files -- the
            # sidecar would not record the keys just pushed, [settings]
            # emptying (invariant 2) and `export` (invariant 6) would both
            # miss them, and it never self-heals: the same key is still
            # unsupported on the next run, so there is still a remainder.
            # A home-manager activation only ever runs --unattended, so
            # that state would be permanent there.
            raise _live.LiveApplyUnsupported("Brave", remainder)

        has_pref_changes = any(
            not plan.empty and plan.namespace in {"settings", "shortcuts"}
            for plan in plans
        )
        will_mutate = bool(
            live_newtab
            or live_ordinary
            or (shortcut_script is not None and not shortcuts_unsupported)
        )
        # Back up BEFORE the live half lands, whether or not a remainder
        # follows it.  A split run's offline path is told (via
        # LiveApplyUnsupported.backup_taken) not to take a second one, so
        # invariant 1 still holds at exactly one -- and that one predates
        # the live half, which is what makes `apply --undo` revert both
        # halves.  Backing up afterwards snapshots a file the browser has
        # already flushed the live changes into, so undo could only revert
        # the offline remainder.
        #
        # When nothing lands live the offline path keeps its own backup:
        # its snapshot is taken after the close, so it also captures the
        # flush this one would predate.
        if has_pref_changes and will_mutate:
            _live.backup_preferences(prefs_path)
            backup_taken = True

        _live.apply_external_plans(plans)

        newtab_script = _newtab_script(live_newtab)
        if newtab_script is not None:
            client.navigate(target, _NEWTAB_URL)
            _await_page(client, target)
            client.evaluate(target, newtab_script)

        settings_script = _settings_script(live_ordinary)
        if settings_script is not None:
            client.navigate(target, _SETTINGS_URL)
            _await_page(client, target)
            client.evaluate(target, settings_script)

        # [shortcuts] is independent of [settings]: it runs even when some
        # settings key is headed offline, and is skipped only when its own
        # preflight found the commands bundle unusable.
        if shortcut_script is not None and not shortcuts_unsupported:
            client.navigate(target, _SHORTCUTS_URL)
            _await_page(client, target)
            client.evaluate(target, shortcut_script)

        if remainder:
            # State files stay unwritten: the offline apply that handles
            # the remainder writes them for the whole plan.
            raise _live.LiveApplyUnsupported(
                "Brave", remainder, backup_taken=backup_taken
            )

        _live.write_state_files(plans)
    except CdpError as e:
        # Degrade to the offline path rather than aborting: in a mixed run
        # [pwa] policy is already written, and the offline apply redoes
        # every plan idempotently.  Carry the backup flag for the same
        # reason the remainder path does -- a CdpError raised after the
        # backup must not earn the offline path a second one.
        raise _live.LiveApplyUnsupported(
            "Brave", [f"live apply failed: {e}"], backup_taken=backup_taken
        )
    finally:
        if created:
            client.close_page(target)
    print("ok -- live applied through Brave DevTools endpoint")
