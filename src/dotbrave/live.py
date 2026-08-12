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

# Poll budgets for values that arrive *after* readyState, in JS-side
# attempts of `_POLL_INTERVAL_MS` each.  The New Tab bound is named once
# because two scripts must agree on it: the preflight decides a key is
# unsupported when the store never appears, and the mutating script throws
# when it does not -- a shorter budget in the mutating script would turn a
# key the preflight cleared into a CdpError.
_POLL_INTERVAL_MS = 50
_NTP_POLL_ATTEMPTS = 20
_PROFILE_PATH_POLL_ATTEMPTS = 40


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
    f"for (let attempt = 0; attempt < {_PROFILE_PATH_POLL_ATTEMPTS} "
    "&& !value; attempt++) {"
    f"await new Promise(r => setTimeout(r, {_POLL_INTERVAL_MS}));"
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


def _plan_removals(plans: list[Plan], managed_before: set[str]) -> set[str]:
    """Keys the sidecar says dotbrave manages that the config no longer names.

    Nothing here reads ``Preferences``, and that is the whole point: the
    file can lag the browser by a whole browsing session, so a key applied
    live and then dropped from the config is absent from *both* sides of
    the tree diff and produces no removal at all.  ``plan_apply`` already
    computed exactly this set (``config_managed_keys - target_keys``) and
    ships the other half of the subtraction in
    ``state_payload["managed_keys"]``, so subtracting reconstructs it
    without a new field on ``Plan`` -- one named generically enough to
    hold this would invite ``[shortcuts]`` to pour a different key space
    into it, and a command id arriving at ``_resolve_removals`` as a
    dotted pref path would send every dropped shortcut offline.

    Filtering by namespace is therefore deliberate rather than
    defensive-by-habit, and so is *not* gating on ``plan.empty``: an empty
    plan is precisely the case this exists for.  A malformed payload
    yields no removals rather than raising, the same way
    ``_enrich_prior_values`` reads one.
    """
    out: set[str] = set()
    for plan in plans:
        if plan.namespace != _base_settings.NAMESPACE:
            continue
        payload = plan.state_payload
        if not isinstance(payload, dict):
            continue
        keys = payload.get("managed_keys")
        if not isinstance(keys, list):
            continue
        out |= managed_before - {k for k in keys if isinstance(k, str)}
    return out


_NO_ENTRY = object()


def _enrich_prior_values(
    plans: list[Plan],
    learned: dict[str, tuple[Any, Any]],
    managed_before: set[str],
) -> None:
    """Record the values the preflight read, where they are the *prior* ones.

    ``_capture_prior_values`` reads the on-disk ``Preferences``, and
    Chromium only persists prefs somebody explicitly set -- so a key
    sitting at its compiled-in default is recorded ``{"present": false}``,
    which ``_resolve_removals`` correctly refuses to act on.  The live
    route can do better, because ``getPref`` returned that default
    before this run wrote anything.

    ``learned`` maps each key to ``(value, target_value)`` -- what
    ``getPref`` read, and what this run is about to write for that key.
    Four conditions gate recording it, all must hold:

    1. the key was applied live in this run (the caller passes only
       those);
    2. it was not in the sidecar's ``managed_keys`` before the run --
       once dotbrave has written a key, ``getPref`` normally returns
       dotbrave's own value, and recording that would make a later
       removal restore dotbrave's setting instead of the user's;
    3. its recorded entry is missing or does not already say
       ``present: true``.  This keeps ``merge_prior_values``'s
       first-seen-wins property intact -- this is not a back door
       around it, it fills in entries that recorded no value at all.
       A malformed entry is left exactly as found: we do not know
       what wrote it;
    4. the value ``getPref`` read differs from ``target_value``.
       Condition 2 alone has a gap: inside Chromium's ~10s pref-commit
       window, a second apply of the same key can see the on-disk
       ``Preferences`` -- and hence ``managed_keys``, which is read
       from the same disk state -- still lag a write dotbrave itself
       already made on the previous run.  ``getPref`` then answers with
       dotbrave's own value even though nothing on disk yet says the
       key is managed, so conditions 1-3 alone would learn it as though
       it were the pre-dotbrave default. When the learned value already
       equals the value being written, there is nothing trustworthy to
       learn either way -- it may be a genuine coincidence, in which
       case the key's real default is simply still unknown and the next
       removal costs a restart, same as before this enrichment existed.
       Learning nothing is cheap; learning wrongly would let a later
       removal silently restore dotbrave's setting instead of the
       user's.

    The plan's ``state_payload`` is mutated in place, which is also what
    gets written: the sidecar cannot diverge from what ``plan_apply``
    computed because there is only ever one dict.  A payload without a
    ``prior_values`` mapping is left alone rather than grown one --
    ``plan_apply`` always emits it, so a payload missing it did not come
    from there and is not ours to reshape.
    """
    if not learned:
        return
    for plan in plans:
        if plan.namespace != _base_settings.NAMESPACE:
            continue
        payload = plan.state_payload
        if not isinstance(payload, dict):
            continue
        prior = payload.get("prior_values")
        if not isinstance(prior, dict):
            continue
        for key, (value, target_value) in learned.items():
            if key in managed_before:
                continue
            if value == target_value:
                continue
            entry = prior.get(key, _NO_ENTRY)
            if entry is _NO_ENTRY or (
                isinstance(entry, dict) and entry.get("present") is not True
            ):
                prior[key] = {"present": True, "value": value}


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
        f"for (let attempt = 0; attempt < {_NTP_POLL_ATTEMPTS} "
        "&& unsupported.length; attempt++) {"
        f"await new Promise(r => setTimeout(r, {_POLL_INTERVAL_MS}));"
        "unsupported = missing();"
        "}"
        "return unsupported;"
        "})()"
    )


def _settings_preflight_script(changes: list[tuple[str, Any]]) -> str | None:
    """Probe every ordinary key, and read the value it holds right now.

    The existence answer is what the per-key split needs.  The value is
    what a *later* removal needs: ``getPref`` returns the pref's
    effective value, so for a key nobody has explicitly set it hands back
    the compiled-in default -- the one thing ``_capture_prior_values``
    can never see, because Chromium does not persist unset prefs.  This
    runs on the settings page before any mutation, so the value read here
    predates dotbrave's own write; the caller is responsible for only
    keeping it where that still means "before dotbrave managed the key"
    (see ``_enrich_prior_values``).

    Per-key entries are returned rather than a bare list of unsupported
    keys.  ``_preflight_settings`` still accepts plain strings, which is
    what the New Tab probe returns.
    """
    if not changes:
        return None
    keys_json = json.dumps([key for key, _value in changes], separators=(",", ":"))
    return (
        "(async () => {"
        f"const keys = {keys_json};"
        "if (typeof chrome === 'undefined' || !chrome.settingsPrivate) "
        "return keys.map(key => ({key: key, supported: false}));"
        "const read = key => new Promise(resolve => {"
        "chrome.settingsPrivate.getPref(key, pref => {"
        "const err = chrome.runtime.lastError;"
        "if (err || !pref) resolve({key: key, supported: false});"
        "else resolve({key: key, supported: true, value: pref.value});"
        "});"
        "});"
        "const out = [];"
        "for (const key of keys) {"
        "out.push(await read(key));"
        "}"
        "return out;"
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
        f"for (let attempt = 0; attempt < {_NTP_POLL_ATTEMPTS} "
        "&& unready.length; attempt++) {"
        f"await new Promise(r => setTimeout(r, {_POLL_INTERVAL_MS}));"
        "unready = missing();"
        "}"
        "if (unready.length) throw new Error("
        "'NTP store actions unavailable for ' + unready.join(', '));"
        f"{calls}"
        "await new Promise(r => setTimeout(r, 300));"
        "return true;"
        "})()"
    )


def _read_preflight_result(
    result: Any, unsupported: list[str], values: dict[str, Any],
) -> None:
    """Fold one preflight answer into the unsupported list and the values.

    Two entry shapes are accepted, deliberately: a bare string names an
    unsupported key (what the New Tab probe returns, and the shape a
    half-loaded page can still produce), while a mapping carries the
    settings probe's per-key answer.

    The *support* answer and the *value* answer fail in opposite
    directions. Support fails open: an entry that is neither a string
    nor a dict, or a dict whose ``key`` is not a string, is dropped
    rather than added to ``unsupported`` -- there is no key name to
    record the entry under, so there is no list it could join. The key
    is therefore treated as supported by omission, same as the parser
    this replaced; if it genuinely is not, the mutating script still
    raises and the run degrades to the offline fallback, so nothing
    silently gets a live write it cannot handle. Value fails closed:
    ``value`` is only kept when the entry actually has one, because
    ``pref.value`` can be dropped in serialisation when it is
    ``undefined``, and a null value is not a value any pref could be
    set back to -- a missing or null value just leaves the key out of
    ``values`` rather than recording something unusable.
    """
    if not isinstance(result, list):
        return
    for entry in result:
        if isinstance(entry, str):
            unsupported.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        if entry.get("supported") is not True:
            unsupported.append(key)
            continue
        if entry.get("value") is not None:
            values[key] = entry["value"]


def _preflight_settings(
    client: CdpClient,
    target: dict,
    newtab_changes: list[tuple[str, str, str, Any]],
    ordinary_changes: list[tuple[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """Return the unsupported keys, and the values the probe read.

    The values come from the settings probe only: the New Tab route
    drives store actions, not ``settingsPrivate``, so it has no pref
    value to report.
    """
    unsupported: list[str] = []
    values: dict[str, Any] = {}
    newtab_script = _newtab_preflight_script(newtab_changes)
    if newtab_script is not None:
        client.navigate(target, _NEWTAB_URL)
        _await_page(client, target)
        _read_preflight_result(
            client.evaluate(target, newtab_script), unsupported, {},
        )

    settings_script = _settings_preflight_script(ordinary_changes)
    if settings_script is not None:
        client.navigate(target, _SETTINGS_URL)
        _await_page(client, target)
        _read_preflight_result(
            client.evaluate(target, settings_script), unsupported, values,
        )
    return unsupported, values


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
    # What dotbrave managed *before* this run.  The sidecar is normally
    # only rewritten once the run succeeds, so reading it here usually
    # answers that question.  Two things need the answer: the removal set
    # below, and `_enrich_prior_values`, which must not record the
    # preflight's `getPref` value for a key dotbrave has already written
    # (there `getPref` returns dotbrave's own value, not the user's).
    # Same source `plan_apply` read at build time.
    #
    # "Normally", because of the gap the CdpError handler documents below
    # ("Known gap, accepted"): a run can mutate a key live and then fail
    # before the sidecar is rewritten, leaving this read stale for that
    # key on the next apply. `_enrich_prior_values`'s fourth condition --
    # the value read must differ from the value this run is about to
    # write -- is the backstop for exactly that case.
    managed_before = _base_settings.get_managed_keys(prefs_path)
    # Union, not replacement: neither source subsumes the other.  Only the
    # tree diff sees a dict-valued key the config still names *shrink* (a
    # leaf vanishing under it, so the key never leaves `managed_keys`);
    # only the plan-derived set sees a key whose value never reached the
    # file.  For a scalar key both name the same thing.  For a dict key
    # dropped whole the union carries the dotted key *and* its leaves,
    # and the leaves stay unresolvable so the run goes offline -- exactly
    # what happens today.  Pruning them would instead push a dictionary
    # value through `setPref`, which is untested against settingsPrivate.
    changes, disk_removals = _setting_changes(prefs, target_prefs)
    removals = sorted(set(disk_removals) | _plan_removals(plans, managed_before))
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
        unsupported, learned_values = _preflight_settings(
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

        # No external ([pwa]) plans are applied here, deliberately.  The
        # orchestrator applies every non-empty one before this adapter is
        # called and drops it from `plans`, so a call here could only ever
        # be a no-op -- and it would sit *after* the backup, inverting the
        # ordering the offline path keeps on purpose (privileged write
        # first, so a sudo failure leaves Preferences untouched).

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

        # Only now, past the remainder raise: the values learned above are
        # written to disk in this run or not at all.  When a remainder
        # sends the run offline, the offline apply writes the sidecar from
        # what `plan_apply` computed, so the plan's payload must still be
        # exactly that -- hence no mutation before the raise, and none on
        # the `--unattended` refusal either.  The learned values are
        # discarded in that case; `plan_apply` recomputes `prior_values`
        # from disk on the next run, as before.
        _enrich_prior_values(
            plans,
            {
                key: (learned_values[key], write_value)
                for key, write_value in live_ordinary
                if key in learned_values
            },
            managed_before,
        )
        _live.write_state_files(plans)
    except CdpError as e:
        # Degrade to the offline path rather than aborting: in a mixed run
        # [pwa] policy is already written, and the offline apply redoes
        # every plan idempotently.  Carry the backup flag for the same
        # reason the remainder path does -- a CdpError raised after the
        # backup must not earn the offline path a second one.
        #
        # Known gap, accepted: under --unattended the orchestrator warns
        # and returns on LiveApplyUnsupported without closing the browser,
        # so a CdpError raised after part of the live half already landed
        # leaves those keys unrecorded in the sidecars -- the same hole the
        # remainder path closes by refusing before it mutates.  Unlike that
        # one it self-heals: a CdpError is transient, so the next
        # successful apply reaches write_state_files and records them.
        raise _live.LiveApplyUnsupported(
            "Brave", [f"live apply failed: {e}"], backup_taken=backup_taken
        )
    finally:
        if created:
            client.close_page(target)
    print("ok -- live applied through Brave DevTools endpoint")
