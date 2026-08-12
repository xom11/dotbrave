# CLAUDE.md

Guidance for agents changing this repository. User-facing setup, examples,
and CLI reference belong in `README.md` and runtime `--help`; keep this file
focused on implementation constraints.

## Project

`dotbrave` manages Brave browser customizations from TOML files: managed
tables `[shortcuts]`, `[settings]`, `[pwa]`, applied live when possible with
an automatic offline fallback, on Linux/macOS/Windows across the stable,
beta, and nightly channels. It is a Python 3.11+ stdlib-only CLI package,
extracted from the multi-browser `xom11/dotbrowser` project. The CLI is
single-browser: `dotbrave apply`, not `dotbrave brave apply`.

## Commands

Run from the repository root:

```bash
pip install -e ".[test]"
PYTHONPATH=src python -m dotbrave --help
PYTHONPATH=src python -m dotbrave apply --help
pytest -q

# Regenerate the command-name mapping from upstream brave-core headers.
# Requires an authenticated `gh` CLI.
python scripts/generate_brave_command_ids.py
```

Useful targeted suites:

```bash
pytest tests/test_help.py tests/test_smoke.py
pytest tests/test_unified_apply.py tests/test_settings_apply.py tests/test_pwa_apply.py
pytest tests/test_live_apply.py tests/test_brave_live.py tests/test_apply_live.py
pytest tests/test_export.py tests/test_restore.py tests/test_brave_channel.py
pytest tests/test_platform.py tests/test_macos_pwa_daemon.py
```

## Code Map

- `src/dotbrave/cli.py`: root parser; mounts actions via
  `browser.register(parser)`.
- `src/dotbrave/browser.py`: channel/profile-root resolution, plan
  assembly, `cmd_*` handlers, registration.
- `src/dotbrave/shortcuts.py`, `settings.py`, `pwa.py`: namespace wrappers
  (module-level state kept patchable for tests).
- `src/dotbrave/live.py`: Brave live-apply routes (settingsPrivate, New
  Tab actions, CommandsService).
- `src/dotbrave/utils.py`: Brave `BrowserProcess` config per channel.
- `src/dotbrave/command_ids.py`: generated name<->id mapping.
- `src/dotbrave/_base/`: engine shared with upstream dotbrowser:
  `orchestrator.py` (config loading, unified apply/export/restore engines,
  argparse wiring), `utils.py` (`Plan`, atomic write), `settings.py`
  (dotted keys + MAC refusal + snapshot/allowlist export), `pwa.py`
  (policy storage + macOS daemon), `process.py`, `cdp.py`,
  `live_apply.py`. Helpers dotbrave no longer registers (init, the
  settings/shortcuts/pwa sub-actions) stay here for upstream porting.
- `examples/*.toml`: valid user-facing config samples.
- `tests/`: behavior contracts. Add or update tests alongside behavior or
  CLI-help changes.

## Invariants

Preserve these contracts unless a change explicitly redesigns them:

1. `apply` uses module `Plan` objects and one orchestrated cycle. Validate
   all selected namespaces before committing profile changes; create at most
   one Preferences backup per apply -- one file, wherever it was taken.
   A live apply that is about to mutate takes that one backup *before* its
   live half lands, whether or not the per-key split of invariant 5 leaves
   an offline remainder, and reports it on the exception
   (`LiveApplyUnsupported.backup_taken`) so the offline path skips its own.
   The order is the point: closing the browser for the offline remainder
   makes Brave flush the live half into `Preferences`, so a backup taken
   after that could only ever undo the remainder. When the live path
   mutates nothing (every key unsupported, or it failed before mutating)
   it takes no backup, and the offline path's -- taken after the close, so
   it captures that flush too -- stays the run's only one.
   When an apply closes the browser, it must re-read `Preferences` before
   mutating and committing: the close flushes the browser's own in-memory
   copy over the file, and writing the snapshot taken before the close
   reverts that flush (`profile.exit_type` among it).
2. Missing TOML table means "skip this namespace"; an empty table means
   "remove/reset entries previously managed by dotbrave" -- what "remove"
   does for `[settings]` is route-dependent. Offline removal deletes the
   key from `Preferences`, so Chromium falls back to its compiled default.
   `chrome.settingsPrivate` has no single-pref reset, so live removal
   instead writes back the value the settings sidecar recorded for that
   key the moment dotbrave first started managing it (first-seen, never
   overwritten by dotbrave's own writes), leaving the key present with
   its pre-dotbrave value rather than gone. A key the sidecar recorded as
   `{"present": false}` was never set before dotbrave touched it -- there
   is no value to restore live, so that removal goes to the offline
   remainder instead. The live route avoids recording that marker where
   it can: `getPref` returns a pref's *effective* value, so the settings
   preflight reads the compiled default of a key that is still unset and
   records it as the prior value. Only for a key this run is the first to
   manage, though -- after dotbrave's own write `getPref` returns
   dotbrave's value, and recording that would make removal restore
   dotbrave's setting instead of the user's. An entry recorded by an
   *earlier* run is never touched (`merge_prior_values` is
   first-seen-wins, and that is a rule across runs). An entry this run's
   own `_capture_prior_values` just read off disk is a different matter
   and *is* replaced by the `getPref` value: both are observations of
   the same quantity taken in the same run, and only the file's copy is
   lagged by Chromium's commit timer -- so a user who changed the key in
   the Brave UI minutes ago, then ran `apply`, would otherwise have
   their pre-edit value locked in as "the prior" permanently, and the
   next removal would silently revert them. `live.apply_live`
   distinguishes the two by reading the sidecar's `prior_values` keys
   into `recorded_before` before anything mutates; because
   `merge_prior_values` is `setdefault`, membership decides provenance
   exactly. Accepted in trade, both permanent once written and both
   needing a prior failure to reach: a key dotbrave already wrote live
   but never recorded (an `--unattended` run that hit a `CdpError`
   before `write_state_files`) whose config value has since changed, and
   a deleted or unparseable settings sidecar, can now learn dotbrave's
   own value as the prior. Condition 4 (`getPref`'s answer must differ
   from the value being written) still covers the common shape of both.
   That default-learning trick only covers keys the
   settings preflight actually calls `getPref` for. The ten keys in
   `_NEWTAB_ACTIONS` (`live.py`, e.g. `show_clock`, `show_stats`,
   `show_background_image`) go through the New Tab store instead of
   `settingsPrivate`, so there is no `getPref` value to learn from --
   they keep the `{"present": false}` marker permanently, however they
   are first managed, and removing one always costs a close, an
   offline write and a relaunch.
   Which keys count as removed comes from the settings sidecar, never
   from the on-disk diff alone. A live apply writes into Brave's
   in-memory `PrefService` and Brave commits that on its own timer, so a
   key dotbrave applied live and the config then drops is absent from
   *both* sides of the tree diff and produces no removal at all --
   orphaned at dotbrave's value, out of `managed_keys`, never
   reconsidered. `live._plan_removals` therefore unions a plan-derived
   set (`managed_before` minus each settings plan's
   `state_payload["managed_keys"]`) into the disk-derived one, filtered
   to the `settings` namespace and not gated on `plan.empty`. Union, not
   replacement: only the tree diff sees a dict-valued key the config
   still names shrink. And no new field on `Plan` -- a generic one would
   invite `[shortcuts]` command ids into a key space `_resolve_removals`
   reads as dotted pref paths. The two sets do not share a granularity
   (leaf paths vs the config's dotted key), so a plan-derived key is
   dropped when the diff already produced removals strictly beneath it:
   for a dict-valued key dropped whole the leaves alone are the whole
   removal, exactly as before this union existed, and carrying the dotted
   key too would open a work tab for a run that refuses anyway and push a
   dictionary value through `setPref`. A key whose value never reached
   the file has nothing beneath it there, so it is unaffected.
   `diff_summary` must emit a line for a
   removed key absent from disk (`- <key> (removed; not present on
   disk)`); silence there makes `Plan.empty` true, and an empty plan is
   dropped by the orchestrator's early return, by the sidecar write on
   the `[pwa]`-dirty branch, and by `compute_target_prefs`. The
   consequence to keep in view: a removal with no usable prior value now
   always costs a close, an offline apply and a relaunch, where a
   memory-only key used to silently do nothing. On the memory-only route
   that restart really does reset the key, but only because the close
   flushes the browser's copy and `cmd_apply` re-reads `Preferences`
   afterwards (invariant 1) -- `_pop_value` then deletes it from the
   post-close copy. It is not a universal claim: for a key the user
   deleted from `Preferences` by hand while Brave was closed the browser
   holds nothing to flush, and the cycle only rewrites the sidecar.
   Under `--unattended` there is no cycle at all -- the run warns and
   returns without writing state files, so an unresolvable removal
   recurs on every activation until an apply runs with Brave closed.
   `[shortcuts]` has the same defect and the same shape of fix.
   `brave.accelerators` is committed on the same delayed timer (measured:
   unchanged at t+0 and t+3s, committed by t+15s), so `plan_apply`'s
   removal set must not be filtered by what is on disk, and
   `shortcuts.diff_summary` must emit `- <name> (reset to default; not
   present on disk)` for a removal the file cannot see -- same three
   gates. It needs no union with a disk-derived set: the key space is
   flat (command id -> list of strings), so `sidecar managed_ids -
   target_ids` is complete on its own. Both entry points filter the
   sidecar to numeric string ids (`plan_apply` and
   `live._shortcut_removals`): the set now comes only from a
   user-editable file, and `int(cid)` in `diff_summary` and
   `sorted(key=int)` in the live script are different code paths.
   "Reset" means writing back `brave.default_accelerators[cid]`, never
   unassigning: `live._shortcut_script` takes the removal set as an
   argument, emits those cids from the defaults map *without* consulting
   the disk map (the script's own "current" is `commandsCache.cache`, the
   browser's live state, so a cid already at its default costs zero
   calls), and returns cids with no recorded default as
   `unresolved` -- `shortcuts.<name>` entries that join the remainder and
   go offline. Those must not be folded into `blocked` (a settings key
   filter) or `shortcuts_unsupported` (which gates the whole script), or
   one obscure keybinding disables the resolvable half. There is
   deliberately no `prior_values` twin for `[shortcuts]`:
   `brave.default_accelerators` is browser-written and already means "the
   binding before any override at all", which is what "reset to default"
   should restore -- a `prior_values` twin would restore dotbrave's
   *previous* shortcut instead.
3. `[settings]` must refuse MAC-protected keys found in either `Preferences`
   or sibling `Secure Preferences`. Never make a write that Brave will
   silently reset on launch.
4. `[pwa]` is external managed policy storage. It requires sudo on
   Linux/macOS or Administrator on Windows when changed, writes before the
   Preferences commit, and has no Preferences sidecar.
   On macOS the policy file is kept alive by a root-owned self-healing
   LaunchDaemon (`org.dotbrave.<bundle>.pwa`) installed during the same
   privileged write; an empty `[pwa]` table removes the daemon and its
   support files. Keep `build_heal_script`/`build_launchd_plist` pure and
   `install_self_healing_daemon`/`remove_self_healing_daemon` patchable.
   Two contracts defend the policy file against macOS itself. Some boots
   run a `ManagedClient` reconcile that unlinks the orphan plist from
   `/Library/Managed Preferences` (observed ~28s in; not every boot, and
   not on every machine), and a browser auto-started as a login item that
   reads the empty policy first will uninstall every managed PWA and never
   reload the policy while running. So: `ThrottleInterval` stays pinned to
   1 -- launchd's 10s default loses that race -- with `StartInterval` as
   the dropped-notification safety net; and the policy file carries `schg`,
   which makes the reconcile's `unlink` fail outright. `schg` also blocks
   dotbrave's own writes, so every privileged write path lifts it, writes,
   and re-pins, and teardown must unpin. At `ThrottleInterval` 1 the daemon
   also reacts inside an apply's own write window, so a write must first
   seed the daemon's source plist with the new content (or, for an empty
   table, boot the daemon out) -- otherwise apply races itself and the
   daemon restores the policy being replaced. Whether `schg` actually defeats
   the reconcile is unverified -- the event is intermittent and resisted
   every attempt to trigger it on demand -- so the heal log is the
   instrument: a line in it means the pin failed that boot.
5. Plain `apply` manages live apply. Endpoints bind to `127.0.0.1` and
   remain internal; no public endpoint or force-kill switch is exposed.
   The live/offline split is per key, not per run: everything
   `chrome.settingsPrivate` recognises is applied live, and `[shortcuts]`
   is applied live independently of it, before anything is refused. A
   removal itself applies live too, as `setPref` of the value the settings
   sidecar recorded before dotbrave first managed the key (see invariant
   2) -- resolved before the preflight so the written-back value is probed
   like any other key. Everything live apply cannot do falls back to the
   same thing -- a normal close, a verified offline apply, a relaunch --
   along five paths. Three are per key, and `LiveApplyUnsupported` names
   exactly that remainder: the keys the browser does not recognise,
   removals with no recorded prior value to write back
   (`chrome.settingsPrivate` has no single-pref reset, and a key never set
   before dotbrave touched it has no default to restore -- unconditionally
   now, including a key whose value only ever reached the browser's
   memory, see invariant 2), and -- as a
   block, named by the marker `shortcuts` -- the whole `[shortcuts]` table
   when its own preflight reports the commands bundle unusable. Two are
   whole-run and carry a reason string instead of keys: a work tab whose
   profile cannot be confirmed or does not match (below), and any
   `CdpError`, which the adapter translates rather than letting it abort
   the run, because the offline apply redoes every plan idempotently.
   One unknown key must never drag the keys that would have worked -- or
   `[shortcuts]`, which has nothing to do with it -- offline with it. So
   the shortcut script runs before the raise, and state files stay
   unwritten whenever a remainder exists: the offline apply writes them
   for the whole plan.
   `--unattended` is the exception, and keeps all-or-nothing semantics:
   when a remainder exists it refuses *before* mutating anything (no
   backup, no script, no state file). Nothing closes the browser to finish
   the remainder in that mode, so a live half applied there would never be
   recorded in the sidecars, would leak into invariants 2 and 6, and would
   never self-heal -- the same key is still unsupported next run, so there
   is still a remainder. On a home-manager activation, which only ever
   runs `--unattended`, that state would be permanent.
   Every close captures the running
   command line *first* (`find_cmdline_fn`) and the relaunch forwards its
   flags: rebuilding the command line from scratch drops whatever the
   session needed to start at all, and a Brave launched with
   `--ozone-platform=wayland` on a Wayland-only compositor then dies on
   X11 with no `$DISPLAY` -- closed by us, unable to reopen. If the
   relaunch does fail, `_reopen_after_failed_relaunch` puts the browser
   back; after a write that already printed `applied and verified` that
   failure is a stderr warning and exit 0, because a non-zero exit there
   tells callers the apply failed and invites a retry that closes and
   reopens Brave for nothing.

   Two platform facts the relaunch path depends on, both measured rather
   than assumed. **macOS launches the binary inside the bundle, not
   `open -a`**: LaunchServices can still consider the app running right
   after a close and then drops `--args` entirely, so the browser came
   back with no debugging port — the same command failed once and worked
   on the retry, while exec'ing the binary brought the endpoint up in
   about a second every time. TCC grants follow the bundle, so nothing
   is lost by doing it directly. And **`_read_cmdline` must split the
   flat string** that `ps -o command=` / Win32_Process return: returning
   `[line]` made `line[1:]` empty, which silently disabled both flag
   forwarding and port discovery on macOS and Windows. `shlex` cannot do
   that splitting (nothing is quoted, and both the app path and
   `Application Support` contain spaces) — " --" is the boundary.

   Endpoint discovery has three sources, in order: dotbrave's own
   sidecar, `DevToolsActivePort`, then `--remote-debugging-port` off the
   running command line. The third exists because Brave only writes
   `DevToolsActivePort` for a *dynamic* port (`=0`) — verified on macOS
   and Linux — so a browser started on a fixed port reads as having no
   endpoint and gets closed for nothing.

   The work tab itself carries no profile guarantee: `PUT /json/new`
   creates it via upstream's `ChromeDevToolsManagerDelegate::CreateNewTarget`,
   which resolves `ProfileManager::GetLastUsedProfile()` -- so the tab lands
   in the browser's *last-used* profile, not necessarily the one this run
   targets, while everything else in the run (the diff, the backup, the
   sidecars, `verify_fn`) is bound to `args.profile`. So before any
   preflight, backup, or mutation touches that tab, live apply navigates it
   to `chrome://version` and confirms `#profile_path` resolves to
   `prefs_path.parent`; a mismatch -- or a value that cannot be read at all
   -- degrades the run to the existing close -> offline apply -> relaunch.
   The check fails closed on purpose: the alternative is a silent write
   into the wrong profile that still reports `applied and verified`.

   `[pwa]` never touches the
   running browser and is never gated on what else is dirty: the policy
   is written first and directly (no endpoint bootstrap), and Brave
   loads it at next launch. Gating it on "the diff contains nothing but
   `[pwa]`" made one dirty `[shortcuts]` drop the policy on every
   `--unattended` run, and permanently -- `[shortcuts]` is only cleaned
   by an apply with Brave closed, which `--unattended` never performs.
   External plans are then dropped from the remaining work, because both
   the live adapter and the offline block run `external_apply_fn`
   themselves. When the browser-bound tables are skipped afterwards, the
   stderr line names both halves (`[pwa] applied; [shortcuts] not
   applied`) -- exit 0 is the same either way, so nothing else can.
6. The CLI surface is exactly two actions, `apply` and `export`; new
   capabilities become flags on one of them, not new actions. `export`
   emits `[shortcuts]` diffs against `brave.default_accelerators`,
   `[pwa]`, and a `[settings]` block that unions: currently-managed keys
   (so re-applying the export cannot reset them), allowlisted well-known
   keys (`KNOWN_SETTINGS` in `settings.py`; prefixes only ever get added
   -- Chromium/Brave do not rename shipped pref strings), and -- when an
   `export --snapshot` baseline sidecar exists -- keys changed since the
   snapshot. MAC-protected keys appear only as comments. `export` never
   consumes the snapshot; `apply --undo` does not delete it.
7. `apply --undo` restores the most recent Preferences backup and clears
   shortcut/settings sidecars. If Brave is running, it closes normally and
   restarts; it does not roll back external `[pwa]` policy. After a split
   run that backup is the pre-live one of invariant 1, so undo reverts
   both halves -- the live keys and the offline remainder -- not just the
   remainder.
   "Most recent" is decided by *filename*, not mtime. Every backup name
   comes from one helper (`new_backup_path`) whose fields are all fixed
   width down to the microsecond, so a byte sort of the names is a
   chronological sort, and a name is never reused -- `shutil.copy2`
   truncates its destination, so two applies sharing a second used to
   leave one file. mtime cannot carry this: `copy2` copies
   `Preferences`' mtime onto the backup (now corrected with `os.utime`,
   but every backup written before that still lies), and a profile
   directory copied between machines loses its mtimes while the names
   survive. Old second-resolution names still glob, still restore, and
   still sort correctly.
8. Profile flags (`--channel`, `-r`, `-p`) are accepted both before and
   after the action name: real defaults live on the root parser; action
   parsers re-declare them with `argparse.SUPPRESS` so the after-action
   form overrides. Keep new action parsers consistent with this scheme.
9. Runtime help is part of the capability contract. Do not reintroduce
   manual endpoint-selection or force-kill controls, and do not readvertise
   removed actions.

## Browser Notes

- Shortcut values use Chromium KeyEvent-style bindings. `Meta+` and
  `Command+` are normalized per platform before persistence.
- `--channel` changes both profile discovery and process handling.
  Non-stable Linux channels require PID filtering so applying Beta/Nightly
  does not close another Brave channel.
- Process detection/close/kill are scoped to the target
  `--user-data-dir`, not just the channel. `cmd_apply`/`cmd_restore` call
  `BrowserProcess.scope_to_profile(profile_root, default_root)` before the
  engine runs, so an apply against one profile root never closes a Brave
  the user has open on a different root/profile. On Linux the scope reads
  each `pgrep -x brave` pid's own cmdline: an explicitly-launched Brave
  keeps `--user-data-dir=<root>` on its main process *and* every child,
  while an app-menu launch carries the flag nowhere -- so a flagless pid
  is matched only when the target root equals the default root. An unset
  scope keeps the historical global `pgrep`/`pkill -x` behavior (relied on
  by Snap/Flatpak stable installs, whose paths a filter would exclude).
  macOS (`osascript quit`) and Windows (`taskkill /IM`) close by
  app/image name and remain instance-global.
- "Cannot determine whether the browser is running" is a third state, not
  a synonym for "not running". A missing `pgrep`/`tasklist` raises
  `ProcessStateUnknown` out of `BrowserProcess.running()`; the tools' own
  non-zero exit for "nothing matched" stays an empty list. `pids()` remains
  best-effort and swallows it, because every caller of `pids()` iterates the
  result. `apply`/`restore` refuse to guess: `--unattended` reports and skips
  (exit 0), otherwise the run exits naming the tool — and neither path takes
  a backup, since a backup for a write that never happens only accumulates.
  This is not hypothetical: home-manager's activation PATH holds Nix store
  paths only, so `/usr/bin/pgrep` is invisible there, and the old
  conflated `except` made every activation write Preferences underneath a
  live Brave, which flushed its own copy back over it. `nix/home-manager.nix`
  prepends the process tools' directory for exactly this reason.
- Windows: dotbrave may run outside the interactive desktop session (SSH
  commands land in session 0; the browser's windows live in session 1).
  Window messages and GUI launches do not cross sessions, so
  `close_and_wait` and `_spawn_detached` route through a one-shot
  `schtasks /IT` trampoline (`_run_in_console_session`), and launches go
  through a generated `.ps1` -- an inline `-Command` loses the embedded
  quotes on space-containing args (`--user-data-dir=...`), silently
  splitting them and launching against the wrong profile. After a
  graceful close, windowless background-mode residue is terminated (it
  holds the profile and process singleton); processes with open windows
  are never force-killed -- the error names their window titles. Brave
  on Windows does not write `DevToolsActivePort`, so endpoint discovery
  relies on the `.dotbrave.live.json` sidecar that `apply` writes at the
  profile root after a successful live apply; `find_devtools_port` trusts
  it only while `devtools_endpoint_alive` confirms the port. A later
  `apply` that finds a live port reuses it; once the port is gone (e.g.
  `apply --undo` restarts Brave without the debug flags) the next apply
  re-bootstraps.
- Live apply drives privileged pages in a dedicated work tab
  (`CdpClient.create_page`/`close_page`), never by navigating a user's
  existing tab.
- Keep shared engine logic in `_base/` (it mirrors upstream dotbrowser's
  `_base/`, which eases porting fixes across the two repos); Brave-specific
  behavior belongs in the top-level modules.
- Preserve testability: policy paths, privilege writers, and process
  callbacks are intentionally patchable in tests.

## Release

`src/dotbrave/__init__.py::__version__` is the version source of truth;
`pyproject.toml` reads it through Hatch. Releases are tag-driven through
`.github/workflows/release.yml` after tests pass (needs the
`PYPI_API_TOKEN` repo secret).
