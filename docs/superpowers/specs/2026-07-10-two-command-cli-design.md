# Two-Command CLI + Known-Settings Allowlist — Design

Date: 2026-07-10
Status: approved (user requested: only `apply` and `export`, optimized for
ease of use; implementation authorized without per-step approval)

## Problem

The CLI currently exposes seven-plus actions (`init`, `apply`, `export`,
`restore`, `shortcuts dump|list`, `settings dump|blocked|snapshot`,
`pwa dump`). The user wants exactly two entry points — `apply` and
`export` — because the mental model should be "write my config to the
browser" and "read my config from the browser", nothing else.

Additionally, research on brave-core/Chromium history showed user-facing
pref *names are effectively immutable* (e.g. `bookmark_bar.show_on_all_tabs`
unchanged 2014→2026; `brave.tabs.*` unchanged since creation; ~1-5 pref
migrations per year across all of brave-core, all feature removals, not
renames). A curated allowlist of known user-facing settings is therefore
cheap to maintain and lets `export` emit useful `[settings]` immediately,
with no snapshot required.

## CLI surface (complete)

```
dotbrave apply  CONFIG [-n] [--expect-sha256 HEX] [--allow-http] [profile flags]
dotbrave apply  --undo [-n] [profile flags]
dotbrave export [-o FILE] [-a] [profile flags]
dotbrave export --snapshot [profile flags]
```

Command names stay `apply`/`export` (dotfiles-standard, already the
clearest available).

Folded/removed actions and their replacements:

| Removed              | Replacement                                        |
|----------------------|----------------------------------------------------|
| `init`               | `export -o brave.toml` (export output IS a valid starter config) |
| `restore`            | `apply --undo` (most recent backup; `-n` previews). `--from`/`--list` dropped — backups remain plain timestamped files next to Preferences for manual use. |
| `settings snapshot`  | `export --snapshot` (same sidecar, same semantics; `--snapshot --clear` removes it) |
| `settings dump`      | `[settings]` block of `export` (allowlist + managed keys) |
| `settings blocked`   | MAC-protected keys appear as comments in the `[settings]` block |
| `shortcuts dump/list`| `[shortcuts]` block; `-a/--all-shortcuts` lists every binding with names |
| `pwa dump`           | `[pwa]` block of `export`                          |

`_base` keeps the now-unregistered `cmd_*`/`register` helpers (they mirror
upstream dotbrowser; only dotbrave's registration stops calling them).

## Known-settings allowlist

`KNOWN_SETTINGS` lives in the top-level `dotbrave/settings.py` (it is
Brave-specific; `_base` gets the generic mechanism). Entries are dotted-key
*prefixes* matched by `startswith`; exact keys are just prefixes that match
themselves. Initial list — every entry verified against a real profile
and/or brave-core source:

```python
KNOWN_SETTINGS: tuple[str, ...] = (
    "bookmark_bar.",
    "brave.tabs.",
    "brave.new_tab_page.show_",
    "brave.new_tab_page.hide_all_widgets",
    "brave.location_bar_is_wide",
    "brave.show_side_panel_button",
    "brave.today.should_show_toolbar_button",
    "brave.today.opted_in",
    "brave.wayback_machine_enabled",
    "omnibox.prevent_url_elisions",
    "browser.show_home_button",
)
```

The volatile denylist still applies on top (filters `p3a_*`-style metrics
should a prefix overlap them), and MAC-protected matches demote to
comments.

## Export `[settings]` block (final semantics)

Union, deduped by key, in this order:

1. **managed keys** (sidecar) at current values — round-trip safety
   (invariant 2: applying the export must not reset previously-managed
   keys);
2. **known settings**: allowlist-matched leaf keys present in Preferences,
   at current values — the "use immediately, no snapshot" path.
   (Chromium only persists prefs that were explicitly set, so presence in
   the file approximates "user touched this".)
3. **snapshot diff** (only when a baseline exists): any other key changed
   since `export --snapshot`;
4. **MAC-protected** entries from any group above → comment lines.

The block is always emitted now (allowlist makes it non-empty in
practice); groups get comment headers. Values not representable in TOML →
comments, as today.

## Apply `--undo`

- `apply --undo` routes to the existing `_base` restore logic with
  `from_path=None, list=False`; `-n` maps to restore's dry-run.
- `CONFIG` becomes optional (`nargs="?"`); passing neither CONFIG nor
  `--undo` (or both) is a parser error.
- Behavior contract unchanged from `restore`: newest backup, clears
  shortcut/settings sidecars, closes/restarts a running Brave, does not
  touch `[pwa]` policy, does not delete the snapshot sidecar.

## Collateral

- Root parser mounts only `apply` and `export`; module `register()` calls
  are dropped from `browser.register`. `_INIT_TEMPLATE` is deleted.
- Help text rewritten for the two commands (help remains the capability
  contract — invariant 9).
- README CLI reference collapses to two actions; CLAUDE.md invariants 6-8
  updated (6: export scope incl. allowlist; 7: restore→`apply --undo`;
  8: profile-flag scheme unchanged but the `_needs_profile` exception list
  is now empty — every action reads the profile).
- Tests: `test_init.py`, `test_restore.py`, `test_help.py`,
  `test_smoke.py`, CLI-wiring tests in `test_export.py` /
  `test_settings_snapshot.py` updated to the new surface. Module-level
  logic tests (plan_apply, dump-block builders) keep passing untouched.

## Out of scope

- Auto-snapshot after `apply` (may come later; explicit `--snapshot` is
  the single baseline mechanism for now).
- Removing `_base` helpers that upstream dotbrowser still has.
