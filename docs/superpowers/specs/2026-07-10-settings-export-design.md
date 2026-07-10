# Settings Export via Snapshot Diff — Design

Date: 2026-07-10
Status: approved (design discussed and accepted in-session)

## Problem

`dotbrave export` emits `[shortcuts]` (diff vs `brave.default_accelerators`)
and `[pwa]` (managed policy), but intentionally omits `[settings]` because
Chromium exposes no defaults table for arbitrary prefs — "diff vs default"
is not computable.

Goal: let users change settings in the Brave UI and have `export` capture
those changes as a `[settings]` TOML block, without knowing pref key names
up front.

## Approach (chosen)

Snapshot-first workflow ("record mode"):

```
$ dotbrave settings snapshot        # capture baseline of current profile
# ... change settings in the Brave UI ...
$ dotbrave export                   # [settings] block = diff vs snapshot
```

Rejected alternatives:

- **Managed-keys-only export**: only round-trips keys dotbrave already
  manages; cannot capture UI-made changes (the stated goal).
- **Pristine-profile default diff**: launch Brave with a temp
  `--user-data-dir` to synthesize defaults, diff against it. Retroactive,
  but heavyweight, version-dependent, and very noisy. Could be added later;
  out of scope now.

## Design

### 1. New command: `dotbrave settings snapshot`

- Writes a sidecar next to the profile's Preferences:
  `Preferences.dotbrave.settings-snapshot.json`
  (naming consistent with `Preferences.dotbrave.settings.json`).
- Payload: `{"created": "<ISO-8601 local time>", "prefs": {...},
  "secure_prefs": {...}}`. `secure_prefs` is the sibling
  `Secure Preferences` content (may be absent → stored as `{}`); it is
  captured so MAC-protected UI changes can be *reported* (see §4).
- Per-profile by construction (lives next to that profile's Preferences).
- Overwritten by a subsequent `snapshot`; removed by
  `settings snapshot --clear`.
- `export` does NOT consume the snapshot: export is idempotent and
  repeatable against the same baseline.
- Profile-reading leaf: registers profile flags per the invariant-8 scheme
  (same as `settings dump`).

### 2. Export integration

- `browser.py` gains `_export_settings(args, prefs_path, prefs)` inserted
  in `builders` between `_export_shortcuts` and `_export_pwa` (matches the
  init-template namespace order).
- No snapshot present → builder returns `None`; export output is unchanged
  from today except the header note, which switches from "[settings] is
  intentionally NOT exported" to a pointer at `dotbrave settings snapshot`.
- Shared logic lives in `_base/settings.py` (mirrors upstream dotbrowser),
  thin wrapper in the top-level `settings.py` namespace module:
  - `cmd_snapshot(browser_name, args)`
  - `build_export_lines(args, prefs_path, prefs) -> list[str] | None`
  - diff walker + volatile-prefix denylist (module-level constant, kept
    patchable for tests).

### 3. Diff + noise filtering

- Recursive walk of snapshot prefs vs current prefs; differences are
  emitted at **leaf level** as dotted keys:
  - added/changed leaf → `"dotted.key" = <toml value>`
  - leaf removed since snapshot → comment line (Chromium rarely deletes
    keys; no real removal mechanism warranted).
- Volatile-prefix denylist filters subtrees Brave rewrites on its own
  (e.g. `protection`, `sync`, `sessions`, `browser.window_placement`,
  engagement/counter/timestamp subtrees). Session-bounded snapshots keep
  noise low already; the denylist only needs to catch known-always-junk
  prefixes. Stragglers are the user's to delete when reviewing the TOML —
  safe because `apply` still validates and refuses MAC keys.
- Values not representable in TOML (null, unsupported types) → comment
  with JSON payload, same pattern as `settings blocked`.

### 4. Merge semantics (round-trip safety)

Applying a `[settings]` table removes previously-managed keys absent from
it (invariant 2). Therefore, whenever export emits a `[settings]` block it
must be the **union** of:

1. currently-managed keys from the `managed_keys` sidecar, at their
   current Preferences values — so applying the exported file does not
   silently reset them; annotated `# currently managed by dotbrave`;
2. keys that differ from the snapshot; annotated
   `# changed since snapshot <created>`.

Snapshot present but zero diffs → still emit the block with group (1) plus
`# no changes since snapshot`. (A block must be emitted whenever a snapshot
exists, so removals of managed keys stay expressible.)

MAC-protected keys that changed since the snapshot — detected in either
Preferences or the snapshot's `secure_prefs` vs current
`Secure Preferences` — are emitted as **comments** explaining that `apply`
would refuse them (set via UI instead). Never as live keys.

### 5. Collateral updates

- `_EXPORT_HEADER_NOTES` in `_base/orchestrator.py`: replace the
  "intentionally NOT exported" paragraph with the snapshot workflow note.
- `export` and `settings` runtime help text; README.
- CLAUDE.md invariant 6: this design deliberately redesigns that contract —
  update it to describe snapshot-based settings export.
- `restore` does NOT delete the snapshot: it is user-created input, not
  applied state.

### 6. Edge cases

- **Preferences flush lag**: Brave persists Preferences on a ~10s cycle;
  a UI change may not be on disk immediately. Help/output notes: wait a
  few seconds or quit Brave before exporting.
- Corrupt/unreadable snapshot file → export fails with a clear error
  naming the file (do not silently skip a baseline the user created).
- Snapshot taken on one profile does not affect exports of another.

### 7. Testing

- New `tests/test_settings_snapshot.py`: snapshot create/overwrite/clear;
  diff walker (added/changed/removed leaves, nested dicts, lists); denylist
  filtering; TOML emission incl. comment fallbacks; MAC-protected → comment
  (both Preferences- and Secure-Preferences-resident MACs).
- `tests/test_export.py` additions: no snapshot → byte-identical current
  behavior (modulo header note); snapshot + mutated prefs → correct
  `[settings]` block; managed-keys union; zero-diff block; corrupt
  snapshot error.
