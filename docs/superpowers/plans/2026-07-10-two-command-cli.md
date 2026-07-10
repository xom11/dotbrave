# Two-Command CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the CLI to exactly `apply` and `export`, fold restore/snapshot into flags, and add a curated known-settings allowlist so `export` emits `[settings]` without a snapshot.

**Architecture:** Registration shrinks in `browser.py`/`orchestrator.py` (module `register()` helpers stay in `_base` unused, mirroring upstream). The allowlist mechanism goes in `_base/settings.py::build_export_lines` via a `known_prefixes` parameter; the Brave-specific `KNOWN_SETTINGS` list lives in top-level `settings.py`. `apply --undo` reuses `cmd_restore`; `export --snapshot [--clear]` reuses `cmd_snapshot`.

**Tech Stack:** Python 3.11+ stdlib; pytest with existing fixtures.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-10-two-command-cli-design.md`.
- Runtime help must describe only the two-action surface (invariant 9).
- `[settings]` block: managed ∪ known ∪ snapshot-diff, deduped, MAC → comments; block always emitted by `export`.
- Keep `_base` cmd_*/register helpers even when unregistered (upstream mirror).
- Tests run: `PYTHONPATH=src .venv/bin/pytest -q`.
- Conventional commits, no Co-Authored-By.

---

### Task A: Known-settings allowlist in the export block

**Files:** Modify `src/dotbrave/_base/settings.py`, `src/dotbrave/settings.py`, `tests/test_settings_snapshot.py`, `tests/test_export.py`.

**Interfaces:**
- `_base.build_export_lines(browser_name, args, prefs_path, prefs, known_prefixes: tuple[str, ...] = ()) -> list[str]` — no longer returns None; always emits the block. Known group = leaf keys whose dotted key starts with any prefix, present in Preferences, not volatile, not managed, MAC → blocked comments.
- `dotbrave.settings.KNOWN_SETTINGS: tuple[str, ...]` (list from spec) and wrapper passes it.

Block layout (groups omitted when empty; `seen` set dedupes):

```
[settings]
# currently managed by dotbrave:
...
# current values of well-known settings:
...
# changed since snapshot <created>:        <- only when a snapshot exists
...
# MAC-protected -- `apply` would refuse these; set them in the Brave UI:
#   ...
```

When every group is empty, append `# (no known settings found in this profile)` so the block stays informative. Snapshot-diff keys skip keys already in `seen`.

- [ ] Update failing tests first: `test_export_lines_none_without_snapshot` becomes `test_export_lines_known_settings_without_snapshot` (fixture key `bookmark_bar.show_tab_groups` appears; `browser.show_home_button` demoted to MAC comment; returns a block, not None). Add `test_known_prefix_metrics_still_filtered` (volatile leaf under an allowed prefix stays hidden — seed `bookmark_bar.last_visit`). In `test_export.py`, `test_brave_export_no_settings_without_snapshot` becomes `test_brave_export_settings_block_without_snapshot` asserting `doc["settings"] == {}` for the shortcuts-only fixture (block emitted, empty). All previous snapshot tests keep passing (they seed a snapshot).
- [ ] Implement; run `tests/test_settings_snapshot.py tests/test_export.py`; commit `feat: emit well-known settings in export without a snapshot`.

### Task B: `export --snapshot [--clear]`

**Files:** Modify `src/dotbrave/_base/orchestrator.py` (export parser), `src/dotbrave/browser.py` (`cmd_export` routing), `tests/test_settings_snapshot.py`.

- Export parser gains `--snapshot` (capture/refresh baseline and exit) and `--clear` (with `--snapshot`: delete baseline). `cmd_export`:

```python
def cmd_export(args: argparse.Namespace) -> None:
    if getattr(args, "snapshot", False) or getattr(args, "clear", False):
        if not getattr(args, "snapshot", False):
            sys.exit("error: --clear requires --snapshot")
        settings_mod.cmd_snapshot(args)
        return
    _base_cmd_export(...)
```

- [ ] Update CLI wiring test: `parse_args(["export", "--snapshot"])`, `["export", "--snapshot", "--clear"]`; `["settings", "snapshot"]` must fail after Task C (assert there).
- [ ] Implement + commit `feat: fold settings snapshot into export --snapshot`.

### Task C: `apply --undo` + two-action registration

**Files:** Modify `src/dotbrave/browser.py`, `src/dotbrave/_base/orchestrator.py`, `src/dotbrave/cli.py`.

- `browser.register`: `cmd_init_fn=None`, `cmd_restore_fn=None`, `module_registers=[]`; delete `cmd_init` and `_INIT_TEMPLATE`.
- `cmd_apply` head:

```python
    if getattr(args, "undo", False):
        if getattr(args, "config", None):
            sys.exit("error: --undo takes no CONFIG argument")
        args.from_path = None
        args.list = False
        cmd_restore(args)
        return
    if not getattr(args, "config", None):
        sys.exit("error: CONFIG is required (or pass --undo)")
```

- Orchestrator apply parser: `config` becomes `nargs="?"`, add `--undo` flag; description gains an Undo paragraph (timestamped backups next to Preferences; restores newest; clears shortcut/settings sidecars; `[pwa]` policy and the export snapshot are untouched). Export description rewritten: allowlist + snapshot workflow + `-a` as the shortcut-name discovery path. `cli.py` docstring/description/epilog: two actions only, workflow `export -o brave.toml` → edit → `apply`; undo via `apply --undo`.
- [ ] Implement + run full suite (expect fallout fixed in Task D) + commit `feat!: collapse CLI to apply and export`.

### Task D: Test-surface migration

**Files:** Delete `tests/test_init.py`; modify `tests/test_restore.py`, `tests/test_help.py`, `tests/test_smoke.py`, `tests/test_error_messages.py` (if it names removed actions).

- `test_restore.py`: `_restore()` helper drives `brave_pkg.cmd_apply` with `undo=True, dry_run=..., config=None` for default-path tests; `--from`-specific tests keep calling `brave_pkg.cmd_restore` directly (function contract). `test_cli_restore_help_lists_flags` → `restore --help` exits nonzero; new `apply --help` lists `--undo`.
- `test_help.py`: root help lists only apply/export and no removed action names; `_help("apply")` mentions `--undo` and live apply; `_help("export")` mentions well-known settings, `--snapshot`, `[pwa]`; `shortcuts`/`settings`/`pwa`/`init`/`restore` invocations exit nonzero.
- `test_smoke.py`: `shortcuts list|dump` smoke tests become `export`/`export -a` runs on the real profile (read-only), asserting a known command name (e.g. `new_tab`) appears with `-a`.
- [ ] Full suite green; commit `test: migrate suites to the two-command surface`.

### Task E: Docs + verification + release prep

**Files:** `README.md` (CLI reference → two actions; workflow section; remove init/restore/dump rows and stale mentions), `CLAUDE.md` (Project blurb + invariants 6-9 + Code Map init-template mention), `src/dotbrave/__init__.py` (minor version bump — breaking CLI change).

- [ ] Full suite; `--help` smoke for root/apply/export; real-profile `export` smoke (allowlist visible); real-profile `apply --undo -n` NOT run (would need backups; skip).
- [ ] Commit `docs: document the two-command CLI` + push.
