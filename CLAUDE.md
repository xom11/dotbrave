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
  assembly, init template, `cmd_*` handlers, registration.
- `src/dotbrave/shortcuts.py`, `settings.py`, `pwa.py`: namespace wrappers
  (module-level state kept patchable for tests).
- `src/dotbrave/live.py`: Brave live-apply routes (settingsPrivate, New
  Tab actions, CommandsService).
- `src/dotbrave/utils.py`: Brave `BrowserProcess` config per channel.
- `src/dotbrave/command_ids.py`: generated name<->id mapping.
- `src/dotbrave/_base/`: engine shared with upstream dotbrowser:
  `orchestrator.py` (config loading, unified apply/init/export/restore,
  argparse wiring), `utils.py` (`Plan`, atomic write), `settings.py`
  (dotted keys + MAC refusal), `pwa.py` (policy storage + macOS daemon),
  `process.py`, `cdp.py`, `live_apply.py`.
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
6. `export` emits `[shortcuts]` diffs against `brave.default_accelerators`,
   `[pwa]`, and -- only when a `settings snapshot` baseline sidecar exists --
   a `[settings]` block: currently-managed keys (so re-applying the export
   cannot reset them) plus keys changed since the snapshot, with
   MAC-protected changes as comments. There is no defaults table for
   arbitrary prefs, so no snapshot means no `[settings]` block. `export`
   never consumes the snapshot; `restore` does not delete it.
7. `restore` restores Preferences backups and clears shortcut/settings
   sidecars. If Brave is running, it closes normally and restarts; it does
   not roll back external `[pwa]` policy.
8. Profile flags (`--channel`, `-r`, `-p`) are accepted both before and
   after the action name: real defaults live on the root parser; action
   parsers re-declare them with `argparse.SUPPRESS` so the after-action
   form overrides. Profile-reading leaves set `_needs_profile` so
   `_normalize_brave_args` skips profile-root resolution for `init` and
   `shortcuts list`. Keep new action parsers consistent with this scheme.
9. Runtime help is part of the capability contract. Do not reintroduce
   manual endpoint-selection or force-kill controls.

## Browser Notes

- Shortcut values use Chromium KeyEvent-style bindings. `Meta+` and
  `Command+` are normalized per platform before persistence.
- `--channel` changes both profile discovery and process handling.
  Non-stable Linux channels require PID filtering so applying Beta/Nightly
  does not close another Brave channel.
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
