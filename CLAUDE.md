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
   one Preferences backup per offline apply. A live apply takes its backup
   only when the run finishes live -- that is, only when the per-key split
   of invariant 5 leaves no offline remainder. A split run therefore takes
   exactly one backup, the offline path's. Backing up before the live half
   as well would make two.
   When an apply closes the browser, it must re-read `Preferences` before
   mutating and committing: the close flushes the browser's own in-memory
   copy over the file, and writing the snapshot taken before the close
   reverts that flush (`profile.exit_type` among it).
2. Missing TOML table means "skip this namespace"; an empty table means
   "remove/reset entries previously managed by dotbrave".
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
   is applied live independently of it, before anything is refused. Only
   the keys the browser does not recognise and the removals (there is no
   single-pref reset) fall back to a normal close, verified offline apply,
   and relaunch, and `LiveApplyUnsupported` names exactly that remainder.
   One unknown key must never drag the keys that would have worked -- or
   `[shortcuts]`, which has nothing to do with it -- offline with it. So
   the shortcut script runs before the raise, and state files stay
   unwritten whenever a remainder exists: the offline apply writes them
   for the whole plan. Every close captures the running
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
   restarts; it does not roll back external `[pwa]` policy.
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
