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
   one Preferences backup per offline apply.
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
5. Plain `apply` manages live apply. Endpoints bind to `127.0.0.1` and
   remain internal; no public endpoint or force-kill switch is exposed.
   Unsupported live settings and removals fall back to a normal close,
   verified offline apply, and relaunch. A diff whose only changes are
   `[pwa]` never touches the running browser: the policy is written
   directly (no endpoint bootstrap) and Brave loads it at next launch.
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
