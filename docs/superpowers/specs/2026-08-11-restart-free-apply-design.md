# Restart-Free Apply — Design

Date: 2026-08-11
Status: proposed (awaiting approval)

## Problem

The user wants `[settings]`, `[shortcuts]` and `[pwa]` changes to take effect
in the running Brave immediately — "exactly like doing it by hand in the
browser" — with no close/relaunch cycle.

That goal is not fully reachable, and it is worth being precise about why,
because the reasons are unevenly distributed. A small, permanent residue is
imposed by Chromium. Everything else is repo-side and fixable without a new
component, a new transport, or a single new CDP call.

Ranked by how often each actually costs the user a restart:

1. **No live endpoint on a Dock-launched Brave.** `orchestrator.py:459-500`:
   with no discoverable port, the run closes Brave and relaunches it once to
   get one. Only after that does `remember_devtools_port` (:530) make later
   applies live. So the *first apply of every browser session* restarts Brave.
   This is the dominant everyday cost and it is fixed by a plist, with no code.
2. **Removals.** Any key dropped from `[settings]` is refused live and sends
   the run offline (`_base/live_apply.py:91-97`).
3. **A dirty unallowlisted key**, which under the current all-or-nothing
   preflight drags the whole run offline — including an unrelated
   `[shortcuts]` change.
4. **`[pwa]`**, always, on macOS.

This design attacks them in that order: the free infrastructure fix first, then
the CLI fixes, then the expensive agent behind a named experiment.

## Evidence base

Measured this session on Brave 151.1.93.134 / Chrome 151, macOS 25.5, against
throwaway profile roots. Facts marked *(inferred)* are not measured and must
not be built on without the stated check.

**CDP `PWA` domain.** Installs into a *running* browser in ~0.7s, no dialog,
no user gesture. Creates the real macOS shim, persists across restarts,
`PWA.uninstall` removes it cleanly. It is a user-level install: no sudo, and
the user can remove it by hand.

**The domain is transport-gated, not Brave-gated.** Over
`--remote-debugging-port` every method returns `-32601 "wasn't found"` — on
Chrome too. Other chrome-layer browser commands (`Browser.getWindowForTarget`)
work fine over TCP, so the chrome layer is dispatching; only `PWAHandler` is
absent. `ChromeDevToolsSession` builds it only when
`channel->GetClient()->AllowUnsafeOperations()`; `DevToolsPipeHandler` returns
true, the HTTP handler's client does not. `--enable-devtools-pwa-handler` is a
red herring — its branch is `#if BUILDFLAG(IS_CHROMEOS)` and it changes
nothing on macOS (tested).

**The browser dies with the pipe.** Closing the pipe fds quits Brave in under
2s. Whoever holds the pipe must outlive the browser. `--remote-debugging-pipe`
and `--remote-debugging-port` do coexist on one launch.

**macOS refuses a second Brave instance.** With one Brave running, launching
the same bundle with a different `--user-data-dir` dies on signal 9, with and
without the pipe. A pipe-owning agent must therefore be the *first* thing to
launch Brave; it can never adopt a session started from the Dock.

**macOS managed PWA policy is machine-wide**, keyed to the bundle, not the
profile. Every Brave launch honours it whatever `--user-data-dir` says. A
throwaway `-r` root does **not** isolate `[pwa]` side effects.

**`settings_private` has no reset.** The IDL declares exactly `setPref`,
`getPref`, `getAllPrefs`, `getDefaultZoom`, `setDefaultZoom`,
`onPrefsChanged`. `PrefObject` exposes no default value — only
`recommendedValue`, which is the policy-recommended value, a different thing.
So `refuse_live_removals` (`_base/live_apply.py:91-97`) is correct as written.

**The shipped example config forces the first apply offline.** Cross-referencing
`examples/all.toml` against Brave's allowlist (`brave_prefs_util.cc`, 115
entries, 63 resolved to pref strings) leaves these keys outside both the
allowlist and `_NEWTAB_ACTIONS`:

```
brave.tabs.vertical_tabs_enabled
brave.tabs.vertical_tabs_collapsed
brave.new_tab_page.show_brave_news
brave.today.should_show_toolbar_button
brave.ai_chat.show_toolbar_button
brave.ai_chat.context_menu_enabled
brave.ai_chat.autocomplete_provider_enabled
brave.rewards.show_brave_rewards_button_in_location_bar
brave.brave_vpn.show_button
```

`brave.tabs.vertical_tabs_collapsed` is the exact key
`tests/test_brave_live.py:165-178` uses as the canonical "settingsPrivate does
not know this" example — and README tells users to run this config. Because
`_preflight_settings` is all-or-nothing (`live.py:253-257` raises with the
whole list), one such key sends the entire apply, including every key that
would have worked and the whole `[shortcuts]` table, through
close → offline → relaunch. *(The four non-`brave.`-namespaced keys in the
example — `browser.custom_chrome_frame`, `bookmark_bar.show_tab_groups`,
`bookmark_bar.show_on_all_tabs`, `auto_pin_new_tab_groups` — were not checked
against upstream `prefs_util.cc`; treat their status as unknown.)*

Scope this claim precisely. `_setting_changes` diffs current against target and
preflights only the **changed** leaves (`live.py:246-248`), so an unallowlisted
key costs a restart when it is *dirty*, not on every run. With the example
config that means the first apply is guaranteed offline, and every later apply
that touches one of those keys; a steady-state apply stays live. The cost is
real but it is a first-apply and drift cost, not a per-run tax.

**`[shortcuts]` live is the only route that works at all.** *(inferred)*
`AcceleratorService::Initialize()` reads `brave.accelerators` once when the
KeyedService is built, with no `PrefChangeRegistrar`, so an offline
`[shortcuts]` write inherently needs a restart to take effect. The
`commands.bundle.js` route is not a shortcut around the offline path — it is
the only path that lands without one.

## The ceiling, per namespace

**`[shortcuts]`** — live, including removals. `live.py:104-113` already calls
both `assignAccelerator` and `unassignAccelerator`. The CLAUDE.md claim that
`[shortcuts]` is only cleaned by an apply with Brave closed does not match this
code and must be corrected.

**`[settings]`** — live for prefs that are both allowlisted *and* observed at
runtime. Two classes can never be live, and the tool must say so instead of
silently closing the browser:

- *Prefs outside the `settingsPrivate` allowlist.* The allowlist is a
  compile-time `TypedPrefMap` with exact string matching, no wildcards, no
  runtime registration. There is no CDP domain for prefs — **the pipe unlocks
  nothing here.**
- *Prefs read once at startup.* `setPref` returns true and the value reads
  back correctly, but behaviour changes only after a restart. No API reports
  which prefs these are.

A third class, *removals*, is solvable inside dotbrave and is addressed in
Phase 1.

**`[pwa]`** — on macOS, policy is never re-read by a running process, so it
needs either a restart or the CDP route (pipe, hence an agent).

On **Windows this is measured, not inferred, and the answer is yes** — with a
caveat that matters more than the yes. Run 2026-08-12 on the a14 box (Windows
11, Brave already running, pid 9392 up since 09:31:52): one entry was added to
`HKLM\SOFTWARE\Policies\BraveSoftware\Brave\WebAppInstallForceList`, matching
the format of the twelve already there, and nothing else was touched.

    t+100s   nothing
    t+240s   nothing
    t+480s   nothing
    t+580s   installed

The profile grew `Web Applications\_crx_dnfpoenibinnbbckgbhendmlljoobcfg`
containing `Excalidraw.exe` and its icon, and `Excalidraw.lnk` appeared on the
Desktop — created by the **same** browser process, pid unchanged, no relaunch.

Removal was measured too, by taking the entry back out and watching the same
running process:

    t+120s … t+720s   still installed
    t+840s            uninstalled, and the Desktop shortcut removed with it

So both directions work without a restart, on the same cadence: **install
around ten minutes, uninstall around fourteen**. The browser cleans up after
itself — the profile returned to exactly its 13 app directories and the
shortcut was deleted by Brave, not by hand.

So the docstring at `_base/utils.py:58-69` is right: a running browser does
pick the registry policy up. But the delay is the part worth telling a Windows
user: their `[pwa]` change needs no restart, and it also will not be there when
the command returns. A tool that printed "applied" and left them staring at an
unchanged Start menu for ten minutes would be worse than one that said so.
`apply`'s `[pwa]` message is currently written for the macOS reality ("loaded
at its next launch"); on Windows it should say the change is live but lands on
the browser's policy-refresh cycle, in minutes rather than immediately. Not
changed yet — it needs the per-platform wording worked out, and this session
did not do it.

Linux remains *(inferred)*: `/etc/brave/policies/managed/` is a watched
directory and the same reasoning applies, but nobody has measured it.

## Bugs to fix regardless of this design

These were found while mapping and are independent of the live-apply goal.
They are in scope for Phase 1 because Phase 1 touches the same code.

**B1 — the offline fallback writes a stale Preferences snapshot (data loss).**
`prefs = load_prefs(prefs_path)` runs once at `_base/orchestrator.py:304`.
`graceful_close_fn()` at :549 lets Brave flush its in-memory PrefService over
the file. `backup_prefs` at :555 correctly snapshots the post-flush file, but
`plan.apply_fn(prefs)` at :560 and `write_atomic(prefs_path, prefs)` at :575
use the *pre-close* dict. There is no re-read between the close and the write
(the next `load_prefs` is the post-write verify at :583). Everything Brave
wrote at shutdown, and anything the user changed in-session inside the ~10s
commit window the help text itself documents, is reverted. This is the
mechanism behind the earlier incident where a real apply/relaunch cycle
destroyed profile state. **Fix:** re-read prefs after `graceful_close_fn()` and
rebuild the plans against the fresh copy.

**B2 — two backups in one run: not a bug today, but 1.1 creates one.**
`live.py:264` backs up only *after* preflight succeeds, and every
`LiveApplyUnsupported` is raised earlier (`live.py:248` and `:257`), so a run
cannot currently take both that backup and the orchestrator's at
`orchestrator.py:555`. The split runs introduced by 1.1 do apply live *and*
fall back in the same run, which would take two. The rule 1.1 must honour:
take the live backup only when there is no offline remainder.

**B3 — `[settings]` accepts keys that live in `Local State`.** `[settings]`
addresses `<root>/<profile>/Preferences` only, but `settingsPrivate` is
store-agnostic and routes some prefs to local state
(`browser.enabled_labs_experiments`, `performance_tuning.high_efficiency_mode`,
`hardware_acceleration_mode_previous`). dotbrave writes them into the profile
`Preferences`, and `verify_fn` (`_base/settings.py:493-497`) re-reads the file
dotbrave just wrote — so it confirms its own write, prints
`ok -- applied and verified`, and the browser ignores the pref forever.
Verified as a latent hazard only: no such key appears in `examples/` or
`KNOWN_SETTINGS` today. **Fix:** refuse them with a clear message.

**B4 — the live path has no profile targeting.** *(inferred, needs the check
in Phase 1 testing)* `CdpClient.create_page` issues `PUT /json/new`
(`_base/cdp.py:152-170`) with no `browserContextId` and no profile hint, so the
tab is created in whatever profile the browser considers last-used; the
fallback `_page_target` (`live.py:39-43`) returns the first page target of any
profile. Everything else in the run is bound to `args.profile`. The sidecar
port check (`cdp.py:250-266`) does reject a mismatched profile, but the next
two discovery sources (`DevToolsActivePort`, the command line) are
profile-blind, so the check is defeated by its own fallbacks.

**B5 — stale advertised subcommand.** `_base/pwa.py:566` emits the header
`# Generated by \`dotbrave pwa dump\``, a subcommand that does not exist —
`browser.py:312` passes `module_registers=[]`. Invariant 9 forbids
readvertising removed actions.

## Phase 1 — CLI-side, no new components

Everything the endpoint plist does not already cover, plus the correctness
bugs above.

**1.1 Per-key preflight instead of all-or-nothing.** Split the changed leaves
into *live-capable* and *not-live-capable* using the existing preflight
results, apply the live-capable set immediately, and only fall back for the
remainder. `[shortcuts]` must never be dragged into an offline cycle by an
unrelated `[settings]` key.

The split-run contract, pinned so implementations cannot diverge:

- Live-capable keys are applied live **first**, and their sidecar entries are
  written as managed.
- If a remainder exists and the browser may be closed, the run then performs
  the existing close → offline → relaunch cycle for the remainder only,
  re-reading prefs after the close (B1).
- If a remainder exists and closing is not allowed (`--unattended`), the run
  applies the live half, names the remaining keys on stderr, and exits 0 —
  matching the existing `[pwa]`-partial precedent, where exit 0 is the same
  either way so only the message can carry the distinction.
- A split run never prints an unqualified success line; it names both halves.

**1.2 Live removals via a recorded prior value.** Extend the settings sidecar
(`_base/settings.py:500`, currently `{"managed_keys": [...]}`) to record, for
each key at the moment dotbrave *first* manages it, the value `getPref`
returned before the write. For a key that was never set, that value is the
effective default. Removing the key from TOML then becomes
`setPref(key, recorded_value)` — live. Keep the first-seen value; do not
overwrite it on later applies.

Where the recorded value could come from depends on the route that first
manages the key, and the difference matters:

- *Live first-apply* — `getPref` before the write returns the effective value,
  which for a never-set key is the registry default. That would be the good
  case: the recorded value is exactly what a delete would fall back to.
- *Offline first-apply* — there is no `getPref`. Record the value present in
  `Preferences`, and when the key is absent record an explicit `absent`
  marker. `absent` means "we do not know the default", so a later live removal
  of that key is not possible and it joins the offline remainder under 1.1.

**Both bullets now exist.** `_capture_prior_values` (`_base/settings.py`) still
reads the on-disk `Preferences` on every route — that is the offline bullet,
and it is what a plan carries when it is built. The live bullet is layered on
top of it, in `live.py` rather than in `_base/`, because `settingsPrivate` is
Brave-specific knowledge: `_settings_preflight_script` already called `getPref`
for every changed ordinary key, on the settings page, before any mutation, so
it now returns each key's value alongside the existence answer it was written
for — no extra round trip, no extra navigation. `_enrich_prior_values` folds
those values into the settings plan's `state_payload["prior_values"]` just
before `write_state_files`.

The enrichment is deliberately narrow, because `getPref` returns the pref's
*effective* value: for a key dotbrave has already written, that is dotbrave's
own value, and recording it would make a later removal restore dotbrave's
setting instead of the user's. So a value is kept only when all three hold: the
key was applied live in this run, it was **not** in the sidecar's
`managed_keys` before the run (this run is the first time dotbrave manages it),
and its recorded entry is missing or does not already say `present: true`. The
last condition keeps `merge_prior_values`'s first-seen-wins property intact —
the enrichment fills in entries that recorded no value at all, it never
replaces one. The values are merged into the very dict that gets written, so
the sidecar cannot diverge from what `plan_apply` computed.

Consequence for the split run: state files stay unwritten whenever a remainder
exists (the offline apply writes them for the whole plan), so the enrichment
runs *after* that raise and the values learned on a run that falls back are
simply discarded — `plan_apply` recomputes `prior_values` from disk on the next
run, exactly as before. The same holds for the `--unattended` early refusal,
which mutates nothing.

What is left of the old asymmetry: a key first managed by an *offline* apply
(browser closed, or the run fell back) while absent from `Preferences` still
records `{"present": false}`, and because the capture is first-seen-wins
nothing later corrects it — a live apply that finds the key already in
`managed_keys` will not re-capture it, by design. Removing such a key still
costs a close, an offline write and a relaunch. The common case it fixes is the
one that mattered: on a fresh profile, keys first managed live now record the
real default and can be removed live.

A second asymmetry survives within the live route itself: `_preflight_settings`
discards the New Tab probe's values (`live.py:591-593`) by design, because that
route drives store actions, not `settingsPrivate` — there is no `getPref` call
to read a value from. The ten keys in `_NEWTAB_ACTIONS` (`live.py:21-38` —
`show_clock`, `show_stats`, `show_background_image`, and the rest of the
widgets users actually toggle) therefore keep the `{"present": false}` marker
permanently, however they are first managed. Removing one of them still costs
a close, an offline write and a relaunch.

This is a semantic change and must be documented: offline removal deletes the
key, live removal *resets* it, leaving the key present in `Preferences` with a
default-equal value. Invariant 2 has to say so.

**1.3 Make CDP failures recoverable.** `_base/cdp.py` calls `sys.exit` on any
CDP error, any JS exception and any unreachable endpoint
(`cdp.py:143-146, 202-205, 222-223`), so `orchestrator.py:504`, which only
catches `LiveApplyUnsupported`, never sees them. Raise a typed exception
instead and translate it into `LiveApplyUnsupported` at the adapter boundary,
so a live failure degrades to the offline path rather than aborting a run that
has already taken a backup and possibly written `[pwa]` policy.

**1.4 Harden the page-readiness race.** All three routes navigate and then
`time.sleep(0.5)` (`live.py:231, 239, 271, 277, 283`), but `Page.navigate`
returns as soon as navigation *starts*. The settings preflight then
dereferences `chrome.settingsPrivate` unguarded (`live.py:190-194`) and the
mutating script does the same (`live.py:130-131`), so a slow load turns a
fully live-capable change into exit 1. Wait for a load signal and guard the
dereference the way the NTP probe already does (`live.py:170-172`).

**1.5 Add a preflight for `[shortcuts]`.** `live.py:280-284` has none, so a
renamed bundle export lands in `cdp.py`'s `sys.exit` instead of a fallback.

**1.6 Target the right profile.** Bind the work tab to the profile the run is
operating on, or refuse to live-apply when that cannot be established. Fixes
B4.

**1.7 Free win: apply offline with no restart when the target profile is not
open.** `running_fn` is scoped to the user-data-dir, not the profile. A profile
that is not currently open in the running browser has its `Preferences`
untouched by Chromium and can be written offline, right now, with a real
`verify_fn` and zero restart. Check for this before deciding to close anything.

**1.8** Fix B1, B2, B3, B5. Add `--remote-debugging-pipe` to
`_LIVE_OWNED_FLAGS` (`_base/process.py:660-665`) now, ahead of any agent work
— see Phase 3 for why this is a landmine rather than tidiness.

## Phase 2 — a LaunchAgent that only supplies an endpoint

A user LaunchAgent, `RunAtLoad`, launching Brave with
`--remote-debugging-port=0`. No pipe, no socket, no daemon logic, no dotbrave
code at all — one plist.

This removes the most common restart in practice: the first `apply` against a
Brave started from the Dock, which today has no endpoint and so must be closed
and relaunched to get one.

macOS only. On Linux a fixed `ProgramArguments` would discard session flags
such as `--ozone-platform=wayland`, which is exactly the failure invariant 5
was written about.

## Phase 3 — pipe agent for `[pwa]` on macOS (conditional)

**Do not build this until the experiment below passes.** If it fails, `[pwa]`
on macOS stays on policy + next-launch, and there is no remaining reason to
build an agent at all: settings and shortcuts already work over TCP.

### Gating experiment

On a throwaway `-r` root, one pipe-holding process, one browser launch. Note
that the machine-wide policy means the throwaway root still receives the
managed installs, and that `~/Applications/Brave Browser Apps.localized/` must
be swept afterwards by `CrAppModeUserDataDir`.

1. For a URL currently force-installed by policy, get `manifest.id` via
   `Page.getAppManifest`, then `PWA.uninstall` — *expected to fail*
   (`kPolicy` is not in `kUserUninstallableSources`).
2. Remove the URL from the plist, remove the LaunchDaemon and the `schg` pin
   via the existing `remove_self_healing_daemon` path, restart Brave once.
3. `PWA.uninstall` must now succeed and the shim must disappear.
4. `PWA.install` must produce the app in ~1s with a real shim and no dialog.
5. Quit and reopen Brave: the app must still be there and must **not** be
   force-reinstalled.
6. `PWA.uninstall` again: gone, and still gone after the next launch.

Steps 3–6 green justifies the agent. Any red means stop after Phase 2.

### Result — run 2026-08-12, the gate passes

Run without touching the real policy at all. The steps above call for removing a
URL from `/Library/Managed Preferences`, tearing down the LaunchDaemon and the
`schg` pin, and restarting — that needs sudo and mutates a working setup, and it
turns out to be unnecessary. Every load-bearing question is about *user-level*
installs, which a URL the policy has never heard of answers just as well.
`https://excalidraw.com/` was used against a throwaway `--user-data-dir`, with
the user's Brave closed first (macOS refuses a second instance of the bundle).

| Question | Result |
|---|---|
| Does `PWA.install` produce a real app? | Yes — 1.9s, real shim in `~/Applications/Brave Browser Apps.localized/`, `getOsAppState` reports it |
| Does it survive a browser restart? | Yes |
| Does the policy leave it alone? | Yes — not force-removed, no duplicate shim |
| Does `PWA.uninstall` remove it for good? | Yes — shim gone, `getOsAppState` errors |
| Still gone after another restart? | Yes |

Step 1 — whether CDP can uninstall an app the *policy* installed — was not
settled by the run: `Page.getAppManifest` on `https://keep.google.com` resolved
to a Google sign-in redirect rather than the app's own manifest id, so the
`PWA.uninstall` that followed acted on an id that was never installed and
proved nothing. Upstream source settles it instead, and more definitively than
a probe could: `WebAppManagement::kPolicy` is listed in
`kNotUserUninstallableSources` (`chrome/browser/web_applications/web_app_management_type.cc`),
whose counterpart `kUserUninstallableSources` (`…/web_app_management_type.h`)
does not contain it, with a `static_assert` requiring every management type to
appear in exactly one of the two. A policy-installed app is therefore
un-uninstallable by any user-level path, CDP included.

So the two mechanisms cannot share a URL, which is not a limitation to work
around but the thing that decides the design: if `[pwa]` moves to CDP, the
managed policy becomes an **explicit opt-in mode** for enforcement, and the
existing forced entries must be removed wholesale rather than migrated one at a
time. There is no coexistence path.

The macOS side effect stands and is worth restating for whoever runs this next:
the policy is machine-wide, so the throwaway root still received all 12 forced
installs and minted 12 duplicate shims. They were swept afterwards by
`CrAppModeUserDataDir`; expect to do the same.

**Phase 3 is unblocked.** What remains before building it are product decisions,
not technical unknowns — see below.

### Decision, 2026-08-12: Phase 3 is not being built

The gate passed, and the answer is still no. The owner changes the `[pwa]` list
rarely — a couple of entries a year. Phase 3's price is a background process
that owns the browser's lifetime, a one-off migration that removes all twelve
apps and reinstalls them, and the loss of the property that a hand-removed app
comes back by itself. That buys back one close/relaunch every few months.

So `[pwa]` stays on managed policy, and a `[pwa]` change keeps landing at the
browser's next launch. That is now the *only* namespace with that cost:
`[settings]` and `[shortcuts]` — the ones edited often — apply live, including
removals.

Reopen this only if one of these changes:

- the `[pwa]` list starts churning (several changes a month, or a workflow that
  rewrites it programmatically);
- multiple machines need identical PWA sets and the migration cost amortises;
- Chromium gains a way to make a running browser re-read the policy, which
  would deliver the same result with none of Phase 3's cost — that would make
  the agent unnecessary rather than worthwhile, so watch for it before building
  anything.

Everything needed to build it later is recorded above: the transport gate, the
pipe's lifetime coupling, the macOS single-instance rule, and the measured
behaviour of `PWA.install`/`uninstall`. Nothing here needs re-deriving.

### Open decisions, had Phase 3 gone ahead

- **Does `[pwa]` switch wholesale, or is CDP opt-in?** Switching means removing
  the 12 forced entries, the LaunchDaemon, the `schg` pin and the sudo prompts —
  and giving up enforcement: the user can then remove an app by hand and
  dotbrave will not put it back until the next apply.
- **What happens to the 12 apps already installed by policy?** They cannot be
  converted in place. The migration is: remove the policy, restart once, then
  install them again over CDP.
- **Is a browser whose lifetime is owned by a LaunchAgent acceptable day to
  day?** The pipe dies with its owner and the browser dies with the pipe, so
  the agent must outlive Brave, and Brave must be started by it. A Brave the
  user opens from the Dock has no pipe and no live `[pwa]`.

### Shape, if it proceeds

Not a pure relay. When the agent owns the browser process, the CLI must stop
closing and relaunching that process — and the offline fallback, which Phase 1
keeps for unallowlisted prefs, *must* close and relaunch. The two cannot both
be true, so the agent has to own lifecycle, not just bytes.

**Lifecycle agent + CDP relay.** The socket exposes three verbs, all about the
*process*, none about configuration:

- `status` — is Brave running, does it have a pipe, what is the TCP port
- `restart` — close gracefully, wait, relaunch with the pipe; replaces
  `graceful_close_fn` + `launch_live_fn` while the agent is in charge
- `relay` — forward CDP bytes 1:1 over the pipe, one client at a time, no
  message-id rewriting (serialising clients removes the need)

All `[pwa]`/`[settings]`/`[shortcuts]` semantics stay in the CLI with the
existing contract tests. The boundary is what makes this maintainable: the
agent needs no change when Brave renames an NTP store or the commands bundle,
and the CLI's tests keep standing.

Agent install/uninstall/status are flags on `apply`, not new actions
(invariant 6).

### Why the pipe flag is a landmine today

`_forwardable_flags` (`_base/process.py:667-690`) forwards every `-`-prefixed
argument off the captured command line except the four in
`_LIVE_OWNED_FLAGS` — which does not include `--remote-debugging-pipe`
(verified). A Brave launched by the agent with a pipe that then hits any
fallback gets closed, and the relaunch forwards the pipe flag into a process
spawned with `stdin=DEVNULL` and no fd 3/4. Brave starts, reads EOF, quits in
~2s. `_reopen_after_failed_relaunch` then calls `restart` with
`captured_cmdline[1:]` verbatim — pipe flag included — and it dies again. The
user loses the browser and the run exits 0. Hence 1.8.

Second-order: after any fallback relaunch the browser has no pipe, because a
short-lived CLI cannot give it one. Live `[pwa]` capability degrades silently
until the next login. The tool must report this rather than appear to still
have it.

### The `[pwa]` semantics decision

Policy and CDP install are not two routes to one outcome; they are different
products. Policy force-installs machine-wide and cannot be removed by the
user. A CDP install is user-level, per-profile, removable by hand.

They also cannot coexist for the same URL: an app installed by policy is not
user-uninstallable, and on macOS the LaunchDaemon and `schg` pin this repo
installs specifically to survive the boot race will re-assert the policy
afterwards. "Policy as fallback, CDP as fast path" is a trap with a root
daemon defending it. If `[pwa]` moves to CDP, policy becomes an **explicit
opt-in mode** for enforcement, never an automatic fallback.

Note what the original request actually asks for: *"exactly like doing it by
hand in the browser"* is, precisely, a user-level install. Accepting that
retires the entire macOS defence layer — repeated sudo per apply, `schg`, the
LaunchDaemon, the boot race, the heal log. For a single-user dotfiles machine
that is a good trade; the user's own notes record that the policy layer has
already caused pain.

A live `[pwa]` route also needs a sidecar mapping URL → manifestId, because
today the policy file *is* the record of what dotbrave manages
(`_base/pwa.py:485-516`, `state_path=None`). And `entry_for` carries
`default_launch_container: window` and `create_desktop_shortcut`
(`_base/pwa.py:28-31`), which `PWA.install` has no equivalent for — live
installs would lose those semantics.

## Transport

TCP stays for `[settings]` and `[shortcuts]`; the pipe is a narrow channel for
`PWA.*` only.

The three existing routes need a *page session*. Over a pipe that means
`Target.createTarget` + `Target.attachToTarget` + `sessionId` routing on a
persistent NUL-framed stream with increasing ids, while `CdpClient._command`
(`_base/cdp.py:209-226`) opens a fresh WebSocket per command and always sends
`id: 1`. That model works on TCP and breaks on a pipe, and `_WebSocket`
`sys.exit`s on any non-`ws://` URL (`cdp.py:26-33`). Porting them buys nothing
functionally.

`PWA.install`/`uninstall`/`getOsAppState` are browser-level and need no
WebContents, so a pipe client for them is a few dozen lines with no Target or
session routing. Endpoint discovery gains "ask the agent" as a first source,
with the three existing sources demoted to fallbacks.

## Sequencing and plan scope

The phases are numbered by size, not by order of work. Ship them in this
order:

1. **Phase 2's plist**, first and on its own. It is one file, it needs no code
   review, and it removes the most frequent restart. Do it before anything
   else so the rest of the work is measured against a browser that already has
   an endpoint.
2. **Phase 1**, as one implementation plan. B1 (data loss) leads: it is a
   correctness bug on the path everything else falls back to.
3. **Phase 3's experiment**, then Phase 3 only if it passes.

The implementation plan that follows this spec covers **Phase 2's plist and
Phase 1 only**. Phase 3 gets its own spec if and when the experiment justifies
it — its design there is a sketch to be re-derived against measurements, not a
committed plan.

## Invariant amendments

- **1** — already violated by B2; also relax "validate all namespaces before
  committing" to permit per-key partial apply, and require the post-close
  re-read from B1.
- **2** — state the route-dependent meaning of an empty table: offline deletes
  the key, live resets it to the recorded value.
- **3** — the MAC refusal is a defence for *direct disk writes*; a live
  `setPref` goes through PrefService, which recomputes the MAC legitimately.
  Relaxing it for the live route requires plans to become route-aware, since
  the refusal is a `sys.exit` in `plan_apply` that runs before the
  is-running check. Low priority: the tracked MAC set on this machine costs
  only `browser.show_home_button` and `brave.ad_block.*`, and nothing in
  `examples/`.
- **4** — rewritten only if `[pwa]` moves to CDP; policy becomes opt-in, and a
  URL → manifestId sidecar appears where the invariant currently says there is
  none.
- **5** — add the pipe flag to `_LIVE_OWNED_FLAGS`; when the agent owns the
  process, close/relaunch delegates to it; the clause "`[pwa]` never touches
  the running browser" inverts.
- **6** — agent management is flags on `apply`. Add a flag that answers "will
  this apply close my browser": `--dry-run` currently returns at
  `orchestrator.py:343` before the running-state check, so it cannot answer.
- **7** — the `--undo` remedy text changes if `[pwa]` becomes user-level.
- **9** — help text promising "closing it normally and relaunching once for
  live apply" becomes conditional. Fix B5.

## Testing

Phase 1 is unit-testable with the existing fake-CDP harness. Tests that assert
all-or-nothing fallback (`test_brave_live.py:165-178`,
`test_live_relaunch.py:178`) change to assert *partial* apply plus a named
remainder. New tests: per-key split, prior-value recording and replay on
removal, post-close re-read (B1), Local State refusal (B3), single backup (B2),
profile-targeted work tab (B4).

Two experiments need a real browser and must run with the user's Brave closed
(macOS allows no second instance) on a throwaway root, sweeping
`~/Applications/Brave Browser Apps.localized/` by `CrAppModeUserDataDir`
afterwards:

- a `getPref` sweep over every key in `examples/all.toml` and the user's own
  config, to replace the source-derived allowlist inference with measurement
  and to settle the four non-`brave.` keys;
- the Phase 3 gating experiment.

## 1.7 was attempted and reverted — the signal cannot prove a profile is closed

Implemented as commit `67057c3`, reverted in `733900a` after review. Recorded
here so nobody rebuilds it the same way.

The idea was sound; the available signal is not. Reading `--profile-directory`
off the running browser's command line is a **lower bound** on the set of
loaded profiles, never a proof that a profile is unloaded:

- Chromium's `ProcessSingleton` is keyed on `--user-data-dir`, so there is one
  browser process per root serving *every* profile opened under it. A second
  launch hands its arguments to the running process through the singleton and
  exits; the running process opens the requested profile itself and its own
  argv never changes. Opening a profile from the avatar menu adds no argv at
  all, and session restore reopens every profile in
  `profile.last_active_profiles` from one command line or none.
- Worse, a flagless launch does not mean "Default". With no
  `--profile-directory`, Chromium opens `profile.last_used`. A flagless launch
  is the everyday Linux case — the shipped desktop entry is
  `brave-browser-stable %U` — so a user living in `Profile 1` who starts Brave
  from the app menu and runs `dotbrave apply -p "Profile 1"` would have been
  told the profile was closed, and dotbrave would have written the
  `Preferences` of a profile visible on screen.

That is the silent-undo class this project has already been bitten by, and the
`verify_fn` that follows would not catch it: it re-reads dotbrave's own bytes
while the browser still holds its copy in memory. Note also that a loaded
profile's prefs are committed on a ~10s timer throughout the session, not only
at close — so "the browser only flushes on close" is not a safe premise for any
future attempt either.

The optimisation is only implementable with a signal that gives **positive
proof a profile is unloaded**. Two Linux-only candidates, both unverified and
both deserving their own spec: scanning `/proc/<pid>/fd` of every scoped Brave
pid for descriptors under `<user-data-dir>/<profile>/`, or `F_GETLK`-probing
the leveldb `LOCK` files a loaded profile's storage backends hold. Neither has
a macOS or Windows equivalent, where the callback would stay a constant `True`.

Until such a signal exists, the honest answer is that a running browser on the
target root means the profile may be open, and the close stands.

## Found after shipping, then fixed: a removal was computed against the disk

First end-to-end run against a real Brave, 2026-08-12, on a throwaway root.
Three applies in sequence: add four keys, flip one, drop one. The first two
behaved exactly as designed — the Dock-launched Brave was closed and relaunched
once to obtain an endpoint, and the second apply was live with no restart. The
third exposed a bug the unit tests had missed, because nothing modelled the
thing that causes it: **the browser holds state the disk does not.**

A live apply writes into Brave's in-memory `PrefService`. Brave commits that to
`Preferences` on its own timer (~10s) or at exit. Every backup taken during the
run proves the gap — all three show the four keys still `ABSENT` on disk, while
the file written when Brave finally quit has them.

`_setting_changes` diffs the **on-disk** prefs against the target. So when the
config drops a key whose value has only ever existed in browser memory,
`changed_leaf_paths` sees `MISSING` on both sides and reports no change at all.
The key never enters `removals`, `_resolve_removals` never runs, and nothing is
restored — yet `plan_apply` still writes the new `managed_keys` without it. The
key is orphaned: left at dotbrave's value, no longer managed, with a
`prior_values` entry that can never be used again.

Measured: `brave.enable_closing_last_tab` had a recorded prior value of `true`,
was dropped from the config, and finished at `false` — dotbrave's own value.
The run printed `ok -- live applied` throughout.

It self-corrects for a removal issued after Brave has flushed: the key is then
on disk, the diff sees it, and the prior value is restored correctly. So the
window is "remove a key before the browser has committed the write that set
it". Narrow in wall-clock terms, but the very first natural sequence hit it,
and a browser killed before it flushes widens it to "forever".

This is the same family as B1 — the live path trusting a disk snapshot when the
running browser is the authority — surviving in a third place after being fixed
in two.

### What shipped

Two changes, because the bug turned out to have two independent sources. Only
the first is Brave-specific; the second sits in `_base/` and is what the family
had in common.

**A. `live.apply_live` unions a plan-derived removal set into the disk-derived
one.** The set is `managed_before` (already read from the sidecar for
`_enrich_prior_values`; the read simply moved above the diff) minus each
settings plan's `state_payload["managed_keys"]` — the other half of the
subtraction `plan_apply` had already computed as `config_managed_keys -
target_keys`. Nothing in it reads `Preferences`, which is the point.

Two things it deliberately is not. It is **not** a replacement for the disk
branch: only the tree diff sees a dict-valued key the config still names
*shrink*, and a run whose only work is one vanished leaf would otherwise print
`ok -- live applied` having written nothing (measured: replacing rather than
unioning breaks seven test ids, none of which assert the bug). And it is **not**
a new field on `Plan`: a field named generically enough to carry this would
invite `[shortcuts]` to pour command ids into a key space `_resolve_removals`
reads as dotted pref paths, so every dropped shortcut would force a restart.
The namespace filter in `_plan_removals` is load-bearing for the same reason,
and the helper does not gate on `plan.empty` — an empty plan is precisely the
case it exists for.

The union is over two different granularities, and one shape makes that
visible: the tree diff names leaf paths (`brave.some_dict.b`), the plan names
the config's dotted key (`brave.some_dict`). For a scalar key they coincide.
For a **dict-valued key dropped whole** the diff has already produced every leaf
under it, and the dotted key on top of them is not extra safety — it is a
behaviour change. It resolves out of `prior_values` into a live
`setPref(key, <dict>)`, a dictionary through `settingsPrivate` that nothing here
tests, while its own leaves stay unresolvable, so the run goes offline regardless
and `apply_fn` pops the key back off — undoing the write. And without it that
diff has no live half at all, so it refuses before a work tab is opened, which is
the guarantee `test_removal_only_diff_never_opens_a_work_tab` exists for. So a
plan-derived key is dropped when the disk diff already produced removals
*strictly beneath* it (`any(d.startswith(key + ".") for d in disk_removals)`).
A key whose value never reached the file has nothing beneath it in that diff, so
the case this whole set exists for is untouched. Verified by running the shape
against a detached checkout of the pre-fix commit and against the fix: identical
raise, no work tab, no navigation, no `setPref`, no backup on either.

Resolving against the browser via `getPref` — the direction sketched before the
fix — was **not** taken. It is unnecessary: the sidecar's `prior_values` already
holds what a removal needs, and the preflight cannot be asked about a key until
that key is a removal candidate, which is the actual defect.

**B. `diff_summary` emits a line for a removed key absent from disk** rather
than staying silent (`  - <key> (removed; not present on disk)`). The silence
made `Plan.empty` True, and an empty plan is dropped at three gates that no
change inside `apply_live` can reach: the orchestrator's `no changes --
Preferences already match config` early return; the sidecar write on the
`[pwa]`-dirty branch, which rewrote `managed_keys` for a plan that was never
applied and orphaned the key permanently; and `compute_target_prefs`, which
skips empty plans and so never runs `apply_fn`'s `_pop_value` on the offline
path. `Plan.empty` and `write_state_files` needed no change of their own — with
B in place `.empty` means what it says again.

**Behaviour change worth knowing.** A removal whose recorded prior value is
`{"present": false}` now costs a close + offline apply + relaunch where it
previously did nothing at all. Three routes reach that marker: a key first
applied while Brave was closed and absent from disk, a key the config set to
its own default (`_enrich_prior_values` condition 4 deliberately learns nothing
there), and a key the user deleted by hand.

On the memory-only route the restart is not a tax for nothing — the close
flushes the value dotbrave holds in the browser's RAM to disk, `cmd_apply`
re-reads `Preferences` after the close, and `_pop_value` then deletes the key
from *that* copy, so it genuinely returns to its default. That correctness
depends on the post-close re-read; without it the restart would write the
pre-close snapshot back and undo itself. It does **not** generalise to all three
routes: for a key the user deleted from `Preferences` by hand while Brave was
closed, the browser is holding nothing to flush and the cycle rewrites the
sidecar and nothing else. The close/relaunch buys bookkeeping there, not a reset.

`--unattended` has no cycle to spend. It warns and returns without writing state
files, by design (invariant 5), so an unresolvable removal is *not* consumed:
it recurs on every activation until an apply runs with Brave closed. New for
home-manager users, and the honest version of what used to happen — pre-fix the
key was orphaned silently and forever.

**On the claim that no unit test could reach this.** Wrong, and worth
correcting: `apply_live` never writes `Preferences`, so "the disk lags the
browser" is the fake CDP client's default state, not something it had to grow.
The regression tests are ordinary `FakeCdpClient` tests. The one real trap is
that a **drop-only** config does not reproduce the bug — the plan comes out
empty and `cmd_apply` returns before the adapter is reached — so a test written
that way passes against the buggy code. Each new test flips one key and drops
another.

### Same family, left alone on purpose

- **`shortcuts.py:137`, the `[shortcuts]` twin.** `removed_ids = {cid for cid in
  (config_managed_ids - target_ids) if cid in current}` filters against the
  accelerators read from disk, so a shortcut applied live and dropped inside the
  uncommitted window is never reset, while `write_state_files` drops it from
  `managed_ids` — and `[shortcuts]` has no `prior_values` to recover from.
  Deleting the `if cid in current` filter **alone would be worse than the
  status quo**: `_shortcut_script` decides whether to emit a cid by comparing
  `old_keys` (from the same disk state) with `new_keys`, so an absent cid
  compares equal and is skipped anyway — the run would print `- name: (reset to
  default)`, do nothing, and still write the sidecar. A correct fix drives
  `_shortcut_script` from the removal set instead of the disk diff and guards
  `shortcuts.py:101` against a `KeyError` for a cid not on disk. It also rests
  on a premise nobody can check without opening a browser: whether Brave
  commits `brave.accelerators` on the standard delayed timer or calls
  `CommitPendingWrite` immediately. If the latter, this twin does not exist in
  practice.
- **`live.py:314-318`, `_enrich_prior_values` condition 3.**
  `_capture_prior_values` fabricates `{"present": true, value: <old disk
  value>}` for any key present on disk, `merge_prior_values` folds it in before
  enrichment runs, and condition 3 then refuses to overwrite a `present: true`
  entry — even when the preflight's `getPref` just read the browser's real,
  newer value. Conditions 2 and 4 already exclude the two ways `getPref` can
  echo dotbrave's own value, so the refusal buys nothing here. Consequence: a
  user changes a setting in the UI, runs `apply` inside the uncommitted window,
  and dotbrave locks the value from *before* that change as the prior; a later
  removal silently reverts the user's change. Kept out of this change on
  purpose — fixing it changes how often the `present: false` restart above
  fires, so mixing the two would make any test that changed colour impossible
  to attribute. Its offline sibling is already recorded as deliberately
  deferred at `_base/orchestrator.py:598-616`.

Two more, outside this family, recorded so they are not lost: `--dry-run`
builds its diff from the disk snapshot, so inside the uncommitted window it
over-reports `+` for keys the browser already has (it no longer under-reports
removals — that is change B); and `restore --list` prints the mtime of
`Preferences` rather than of the backup (`_base/utils.py:96` uses
`shutil.copy2`), so the timestamp contradicts the `.bak.%Y%m%d-%H%M%S` filename
printed beside it.

**Closed.** The `restore --list` mtime note is fixed: `backup_prefs` now
`os.utime`s the copy it just made (and `restore_prefs` does the same for the
restored `Preferences`), so a backup's mtime is when it was taken. The same
change gave backups microsecond-resolution names through a single
`new_backup_path` helper — two applies in one wall-clock second used to leave
one file, because `copy2` truncates its destination and returns normally — and
made `cmd_restore` sort by name rather than mtime, since the name is now the
only signal that survives a profile directory being copied around.

### Deliberately not done: dropping the tree diff for `[settings]`

The larger version of this fix computes both changes and removals at dotted-key
granularity (`target.items()` plus `removed_keys`), on the argument that this is
the granularity `apply_fn`, `managed_keys`, `prior_values` and `setPref` all
already share, and that the tree diff is the odd one out. The argument holds and
may well be where this ends up. But it deletes `changed_leaf_paths`,
`split_removals` and `_is_shortcut_path` and rewrites `_setting_changes` — a
redesign, not a fix. The union above is the smallest change that repairs the
measured bug.

## Out of scope

- Making unallowlisted or startup-read prefs live. Not possible; the tool
  should name the keys and ask instead of silently closing the browser.
- Any Linux/Windows LaunchAgent equivalent.
- Verifying the *(inferred)* claim that `[pwa]` is already live on Windows and
  Linux. Worth checking on the a14 box, but it does not block anything here.
- Reworking `[settings]` to address `Local State` as a second store. Phase 1
  only refuses those keys.
