# Snapshot-Based `[settings]` Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `dotbrave settings snapshot` captures a Preferences baseline; `dotbrave export` then emits a `[settings]` block containing keys changed since that snapshot, unioned with keys dotbrave already manages.

**Architecture:** All shared logic goes in `src/dotbrave/_base/settings.py` (mirrors upstream dotbrowser), with a thin wrapper in `src/dotbrave/settings.py` and a builder hook in `src/dotbrave/browser.py`. The snapshot is a JSON sidecar next to `Preferences`. Export diffs snapshot-vs-current at leaf level, filters volatile subtrees via a module-level denylist, comments out MAC-protected keys, and always includes currently-managed keys so applying the exported file cannot reset them (invariant 2).

**Tech Stack:** Python 3.11+ stdlib only. Tests: pytest, in-process command calls with `fake_settings_profile_root` / `fake_profile_root` fixtures from `tests/conftest.py`.

## Global Constraints

- Python 3.11+, stdlib only — no new dependencies.
- Run tests as `PYTHONPATH=src pytest ...` from the repo root (package also importable after `pip install -e ".[test]"`).
- Spec: `docs/superpowers/specs/2026-07-10-settings-export-design.md`.
- Snapshot sidecar name: `Preferences.dotbrave.settings-snapshot.json` (next to `Preferences`).
- `[settings]` block, whenever emitted, MUST include currently-managed keys (invariant 2 round-trip safety).
- MAC-protected keys are never emitted as live TOML keys — comments only.
- `export` never consumes/deletes the snapshot (idempotent).
- Keep module-level names patchable for tests (no closures over the denylist).
- Follow existing comment density/idiom; user-facing strings say `dotbrave`.
- Commit messages: conventional (`feat:`, `test:`, `docs:`), NO `Co-Authored-By` lines.

---

### Task 1: `settings snapshot` command (+ `--clear`)

**Files:**
- Modify: `src/dotbrave/_base/settings.py` (imports at top; new helpers after `_get_managed_keys`, ~line 164; new subparser at end of `register`, before `return sub`)
- Modify: `src/dotbrave/settings.py` (wrapper)
- Create: `tests/test_settings_snapshot.py`

**Interfaces:**
- Consumes: existing `find_preferences`, `load_prefs` (from `dotbrave._base.utils`), `_load_secure_prefs`.
- Produces (used by Task 3):
  - `_snapshot_file(prefs_path: Path) -> Path`
  - `_load_snapshot(prefs_path: Path) -> dict | None` — `None` when absent, `sys.exit` on corrupt; returned dict has keys `created: str`, `prefs: dict`, `secure_prefs: dict`.
  - `cmd_snapshot(browser_name: str, args: argparse.Namespace) -> None`
  - `dotbrave.settings.cmd_snapshot(args)` wrapper.
  - CLI: `dotbrave settings snapshot [--clear]` registered with `profile_args` (leaf scheme, invariant 8).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings_snapshot.py`:

```python
"""Tests for `dotbrave settings snapshot` and the snapshot-diff export.

The snapshot is a JSON sidecar (`Preferences.dotbrave.settings-snapshot.json`)
capturing Preferences + Secure Preferences so `export` can emit a [settings]
block of keys changed via the browser UI since the snapshot was taken.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotbrave import settings as brave_settings
from dotbrave._base import settings as base_settings
from dotbrave._base.utils import find_preferences, load_prefs


def _args(profile_root: Path, **extra) -> argparse.Namespace:
    ns = argparse.Namespace(
        profile_root=profile_root, profile="Default", channel="stable"
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def _snapshot_path(profile_root: Path) -> Path:
    return (
        profile_root / "Default" / "Preferences.dotbrave.settings-snapshot.json"
    )


# ---------------------------------------------------------------------------
# settings snapshot / --clear
# ---------------------------------------------------------------------------

def test_snapshot_writes_sidecar(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    snap = _snapshot_path(fake_settings_profile_root)
    assert snap.exists()
    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["prefs"]["homepage"] == "https://existing-home.example"
    # No sibling Secure Preferences in the fixture -> stored as {}.
    assert data["secure_prefs"] == {}
    assert isinstance(data["created"], str) and data["created"]
    assert "snapshot saved" in capsys.readouterr().out


def test_snapshot_captures_secure_preferences(
    fake_settings_profile_root: Path,
) -> None:
    secure = fake_settings_profile_root / "Default" / "Secure Preferences"
    secure.write_text(json.dumps({"homepage": "https://secure.example"}))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    data = json.loads(
        _snapshot_path(fake_settings_profile_root).read_text(encoding="utf-8")
    )
    assert data["secure_prefs"]["homepage"] == "https://secure.example"


def test_snapshot_overwrites_previous(
    fake_settings_profile_root: Path,
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    prefs["homepage"] = "https://changed.example"
    prefs_path.write_text(json.dumps(prefs))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    data = json.loads(
        _snapshot_path(fake_settings_profile_root).read_text(encoding="utf-8")
    )
    assert data["prefs"]["homepage"] == "https://changed.example"


def test_snapshot_clear_removes_sidecar(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    assert _snapshot_path(fake_settings_profile_root).exists()
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=True))
    assert not _snapshot_path(fake_settings_profile_root).exists()
    assert "removed" in capsys.readouterr().out


def test_snapshot_clear_without_snapshot_is_noop(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=True))
    assert "no snapshot" in capsys.readouterr().out


def test_load_snapshot_absent_returns_none(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    assert base_settings._load_snapshot(prefs_path) is None


def test_load_snapshot_corrupt_exits(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._snapshot_file(prefs_path).write_text("{not json")
    with pytest.raises(SystemExit) as exc:
        base_settings._load_snapshot(prefs_path)
    assert "snapshot" in str(exc.value)


def test_load_snapshot_malformed_payload_exits(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._snapshot_file(prefs_path).write_text(json.dumps({"prefs": 42}))
    with pytest.raises(SystemExit):
        base_settings._load_snapshot(prefs_path)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_snapshot_subcommand_is_registered() -> None:
    from dotbrave.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["settings", "snapshot"])
    assert getattr(args, "_needs_profile", False) is True
    assert args.clear is False
    args = parser.parse_args(["settings", "snapshot", "--clear"])
    assert args.clear is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -v`
Expected: FAIL — `AttributeError: module 'dotbrave.settings' has no attribute 'cmd_snapshot'` (and similar for `_load_snapshot`/`_snapshot_file`; the CLI test fails with argparse error on unknown action `snapshot`).

- [ ] **Step 3: Implement snapshot helpers + command + registration**

In `src/dotbrave/_base/settings.py`, extend the imports at the top of the file:

```python
from datetime import datetime
```

After `_get_managed_keys` (below line 163), add:

```python
def _snapshot_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(
        prefs_path.name + ".dotbrave.settings-snapshot.json"
    )


def _load_snapshot(prefs_path: Path) -> dict | None:
    """Load the export baseline written by `settings snapshot`.

    Returns None when no snapshot exists.  A snapshot the user created
    but that cannot be read is an error, not a silent skip: exporting
    without the baseline would quietly drop the [settings] block.
    """
    snap = _snapshot_file(prefs_path)
    if not snap.exists():
        return None
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(
            f"error: unreadable settings snapshot {snap}: {e}\n"
            "(re-run `dotbrave settings snapshot`, or delete the file)"
        )
    if not isinstance(data, dict) or not isinstance(data.get("prefs"), dict):
        sys.exit(
            f"error: malformed settings snapshot {snap}\n"
            "(re-run `dotbrave settings snapshot`, or delete the file)"
        )
    data.setdefault("created", "?")
    if not isinstance(data.get("secure_prefs"), dict):
        data["secure_prefs"] = {}
    return data


def cmd_snapshot(browser_name: str, args: argparse.Namespace) -> None:
    prefs_path = find_preferences(args.profile_root, args.profile)
    snap = _snapshot_file(prefs_path)

    if getattr(args, "clear", False):
        if snap.exists():
            snap.unlink()
            print(f"removed {snap}")
        else:
            print(f"no snapshot to remove at {snap}")
        return

    prefs = load_prefs(prefs_path)
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "created": created,
        "prefs": prefs,
        "secure_prefs": _load_secure_prefs(prefs_path),
    }
    snap.write_text(json.dumps(payload), encoding="utf-8")
    print(f"snapshot saved: {snap} ({created})")
    print(
        f"Change settings in the {browser_name.title()} UI, then run "
        "`dotbrave export` -- its [settings] block will list what changed."
    )
```

In `register()` in the same file, immediately before the final `return sub`, add:

```python
    s = sub.add_parser(
        "snapshot",
        help="capture a Preferences baseline so `export` can diff [settings]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Capture a baseline snapshot of the selected profile's Preferences (and
Secure Preferences) for `dotbrave export`.

Workflow: run `snapshot`, change settings in the {browser_name.title()} UI,
then run `dotbrave export` -- its [settings] block lists keys that changed
since the snapshot.  The snapshot is kept until overwritten by the next
`snapshot` or deleted with `--clear`; `export` never consumes it.

{browser_name.title()} persists Preferences on a delay (~10s): wait a few
seconds after changing a setting, or quit the browser, before exporting.""",
        epilog="""\
Examples:
  dotbrave settings snapshot
  dotbrave settings snapshot --clear""",
    )
    profile_args(s)
    s.add_argument(
        "--clear", action="store_true", help="delete the stored snapshot"
    )
    s.set_defaults(func=lambda args, bn=browser_name: cmd_snapshot(bn, args))
```

Also update the `settings` group parser in `register()` so help mentions the new action. Replace its `description` string:

```python
        description=f"""\
Inspect general {browser_name.title()} Preferences managed through [settings].

`dump` prints currently managed keys by default, or explicitly requested
dotted paths. `blocked` lists MAC-protected Preferences keys that
`dotbrave apply` refuses rather than writing values the
browser would reset on launch. `snapshot` captures a baseline so
`dotbrave export` can emit [settings] keys changed via the browser UI.""",
```

and extend its `epilog` examples with a `  dotbrave settings snapshot` line.

In `src/dotbrave/settings.py`, after `cmd_blocked`, add:

```python
def cmd_snapshot(args: argparse.Namespace) -> None:
    _base.cmd_snapshot("brave", args)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -v`
Expected: all PASS.

Also run: `PYTHONPATH=src pytest tests/test_help.py tests/test_smoke.py -q`
Expected: PASS (help snapshots may assert action lists — if a help test fails, update its expected text to include `snapshot`).

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/_base/settings.py src/dotbrave/settings.py tests/test_settings_snapshot.py
git commit -m "feat: add \`settings snapshot\` baseline command for export"
```

---

### Task 2: Leaf-level diff walker + volatile-key filter

**Files:**
- Modify: `src/dotbrave/_base/settings.py` (add below the Task 1 helpers)
- Modify: `tests/test_settings_snapshot.py` (append)

**Interfaces:**
- Consumes: `_MISSING` sentinel (module-level, already exists).
- Produces (used by Task 3):
  - `_walk_leaf_diffs(old: Any, new: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any, Any]]` — yields `(parts, old_value, new_value)`; either value may be `_MISSING`.
  - `VOLATILE_PREFIXES: tuple[tuple[str, ...], ...]`, `VOLATILE_LEAVES: frozenset[str]` — module-level, patchable.
  - `_is_volatile(parts: tuple[str, ...]) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_snapshot.py`:

```python
# ---------------------------------------------------------------------------
# _walk_leaf_diffs / _is_volatile
# ---------------------------------------------------------------------------

def _diffs(old: dict, new: dict) -> list[tuple[tuple[str, ...], object, object]]:
    return list(base_settings._walk_leaf_diffs(old, new))


def test_walk_identical_yields_nothing() -> None:
    prefs = {"a": {"b": 1}, "c": [1, 2]}
    assert _diffs(prefs, json.loads(json.dumps(prefs))) == []


def test_walk_changed_scalar_leaf() -> None:
    assert _diffs({"a": {"b": 1}}, {"a": {"b": 2}}) == [(("a", "b"), 1, 2)]


def test_walk_added_nested_subtree_reports_leaves() -> None:
    got = _diffs({}, {"brave": {"tabs": {"vertical_tabs_enabled": True}}})
    assert got == [
        (("brave", "tabs", "vertical_tabs_enabled"), base_settings._MISSING, True)
    ]


def test_walk_removed_leaf_reports_missing_new() -> None:
    got = _diffs({"a": {"b": 1}}, {"a": {}})
    assert got == [(("a", "b"), 1, base_settings._MISSING)]


def test_walk_list_change_is_one_leaf() -> None:
    assert _diffs({"a": [1, 2]}, {"a": [1, 3]}) == [(("a",), [1, 2], [1, 3])]


def test_walk_dict_replaced_by_scalar_reports_at_that_path() -> None:
    assert _diffs({"a": {"b": 1}}, {"a": 5}) == [(("a",), {"b": 1}, 5)]


def test_is_volatile_prefix_and_leaf_names() -> None:
    assert base_settings._is_volatile(("protection", "macs", "homepage"))
    assert base_settings._is_volatile(("sessions", "event_log"))
    assert base_settings._is_volatile(("browser", "window_placement", "left"))
    assert base_settings._is_volatile(
        ("profile", "content_settings", "x", "last_modified")
    )
    assert not base_settings._is_volatile(
        ("brave", "tabs", "vertical_tabs_enabled")
    )
    # `session` (singular) holds real user settings -- must NOT be filtered.
    assert not base_settings._is_volatile(("session", "restore_on_startup"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -q -k "walk or volatile"`
Expected: FAIL with `AttributeError: ... has no attribute '_walk_leaf_diffs'`.

- [ ] **Step 3: Implement walker + denylist**

In `src/dotbrave/_base/settings.py`, add `Iterator` to the `typing` import (`from typing import Any, Iterator`), then add below the Task 1 helpers:

```python
def _walk_leaf_diffs(
    old: Any, new: Any, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    """Yield ``(parts, old_value, new_value)`` for every leaf that differs.

    A leaf is any non-dict value; added/removed subtrees recurse down to
    their leaves so each yielded path is directly usable as a [settings]
    dotted key.  ``_MISSING`` marks a path absent on that side.  A dict
    replaced by a scalar (or vice versa) is reported at the path where
    the shapes diverge.
    """
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            yield from _walk_leaf_diffs(
                old.get(k, _MISSING), new.get(k, _MISSING), prefix + (k,)
            )
        return
    if isinstance(new, dict) and old is _MISSING:
        for k in sorted(new):
            yield from _walk_leaf_diffs(_MISSING, new[k], prefix + (k,))
        return
    if isinstance(old, dict) and new is _MISSING:
        for k in sorted(old):
            yield from _walk_leaf_diffs(old[k], _MISSING, prefix + (k,))
        return
    yield (prefix, old, new)


# Subtrees the browser rewrites on its own; diffs under these prefixes are
# never user-actionable settings.  Note `sessions` (plural, session-restore
# bookkeeping) is volatile while `session` (singular, e.g.
# session.restore_on_startup) holds real user settings.
VOLATILE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("protection",),                  # MAC/HMAC bookkeeping
    ("sessions",),                    # session-restore event log
    ("sync",),                        # sync machinery state, not user prefs
    ("browser", "window_placement"),  # window geometry
    ("in_product_help",),             # IPH counters/timestamps
    ("zerosuggest",),                 # cached omnibox suggestions
    ("media_router",),
    ("gcm",),
    ("google", "services"),           # account bookkeeping
    ("signin",),
    ("invalidation",),
    ("ntp", "num_personal_suggestions"),
    ("profile", "last_engagement_time"),
    ("safebrowsing", "metrics_last_log_time"),
)

# Leaf names that are timestamps/counters wherever they appear (e.g. every
# content-settings exception carries a `last_modified`).
VOLATILE_LEAVES: frozenset[str] = frozenset(
    {"last_modified", "last_visit", "last_visited_time", "last_used",
     "lastEngagementTime", "last_engagement_time"}
)


def _is_volatile(parts: tuple[str, ...]) -> bool:
    for pfx in VOLATILE_PREFIXES:
        if parts[: len(pfx)] == pfx:
            return True
    return parts[-1] in VOLATILE_LEAVES
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/_base/settings.py tests/test_settings_snapshot.py
git commit -m "feat: leaf-level prefs diff walker with volatile-key filter"
```

---

### Task 3: `build_export_lines` — the [settings] export block

**Files:**
- Modify: `src/dotbrave/_base/settings.py` (add after `_is_volatile`)
- Modify: `src/dotbrave/settings.py` (wrapper)
- Modify: `tests/test_settings_snapshot.py` (append)

**Interfaces:**
- Consumes: `_load_snapshot`, `_walk_leaf_diffs`, `_is_volatile`, `_get_managed_keys`, `_get_value`, `_split_key`, `_format_toml_value`, `_all_macs`, `_is_mac_protected`, `_load_secure_prefs`, `_MISSING`.
- Produces (used by Task 4):
  - `_base.build_export_lines(browser_name: str, args: argparse.Namespace, prefs_path: Path, prefs: dict) -> list[str] | None` — `None` when no snapshot; otherwise the `[settings]` block lines (no trailing blank line).
  - `dotbrave.settings.build_export_lines(args, prefs_path, prefs) -> list[str] | None` wrapper.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_snapshot.py`:

```python
# ---------------------------------------------------------------------------
# build_export_lines
# ---------------------------------------------------------------------------

def _snapshot_then_mutate(
    profile_root: Path, mutate
) -> tuple[Path, dict]:
    """Take a snapshot, apply `mutate(prefs)` to Preferences, reload."""
    brave_settings.cmd_snapshot(_args(profile_root, clear=False))
    prefs_path = find_preferences(profile_root, "Default")
    prefs = load_prefs(prefs_path)
    mutate(prefs)
    prefs_path.write_text(json.dumps(prefs))
    return prefs_path, load_prefs(prefs_path)


def test_export_lines_none_without_snapshot(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    args = _args(fake_settings_profile_root)
    assert brave_settings.build_export_lines(args, prefs_path, prefs) is None


def test_export_lines_diff_since_snapshot(
    fake_settings_profile_root: Path, capsys: pytest.CaptureFixture
) -> None:
    def mutate(prefs: dict) -> None:
        prefs["brave"]["tabs"]["vertical_tabs_enabled"] = True   # changed
        prefs["omnibox"] = {"prevent_url_elisions": True}        # added subtree
        prefs["sessions"] = {"event_log": [1, 2, 3]}             # volatile: hidden

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    assert lines is not None and lines[0] == "[settings]"
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is True
    assert doc["settings"]["omnibox.prevent_url_elisions"] is True
    assert "sessions" not in body
    assert "changed since snapshot" in body


def test_export_lines_includes_managed_keys(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    # Simulate a prior apply that manages one key.
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )

    def mutate(prefs: dict) -> None:
        prefs["brave"]["tabs"]["vertical_tabs_enabled"] = True

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    doc = tomllib.loads("\n".join(lines))
    # Managed key present at its current value + the UI-changed key.
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is False
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is True
    assert "# currently managed by dotbrave" in "\n".join(lines)


def test_export_lines_managed_key_not_duplicated_when_changed(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )

    def mutate(prefs: dict) -> None:
        prefs["bookmark_bar"]["show_tab_groups"] = True  # managed AND changed

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)  # would raise on a duplicate TOML key
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is True


def test_export_lines_zero_diff_still_emits_managed_block(
    fake_settings_profile_root: Path,
) -> None:
    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    base_settings._state_file(prefs_path).write_text(
        json.dumps({"managed_keys": ["bookmark_bar.show_tab_groups"]})
    )
    prefs_path, prefs = _snapshot_then_mutate(
        fake_settings_profile_root, lambda prefs: None
    )
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    assert "no changes since snapshot" in body
    doc = tomllib.loads(body)
    assert doc["settings"]["bookmark_bar.show_tab_groups"] is False


def test_export_lines_mac_protected_change_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        # browser.show_home_button is MAC-protected in the fixture.
        prefs["browser"]["show_home_button"] = False

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "browser.show_home_button" not in doc.get("settings", {})
    assert "MAC-protected" in body
    assert "browser.show_home_button" in body


def test_export_lines_secure_prefs_change_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    secure = fake_settings_profile_root / "Default" / "Secure Preferences"
    secure.write_text(json.dumps({"homepage": "https://old.example"}))
    brave_settings.cmd_snapshot(_args(fake_settings_profile_root, clear=False))
    secure.write_text(json.dumps({"homepage": "https://new.example"}))

    prefs_path = find_preferences(fake_settings_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "homepage" not in doc.get("settings", {})
    assert "MAC-protected" in body and "homepage" in body


def test_export_lines_removed_leaf_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        del prefs["some"]["unrelated"]

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "some.unrelated" not in doc.get("settings", {})
    assert "removed since the snapshot" in body


def test_export_lines_unrepresentable_value_is_comment(
    fake_settings_profile_root: Path,
) -> None:
    def mutate(prefs: dict) -> None:
        prefs["weird"] = None  # TOML has no null

    prefs_path, prefs = _snapshot_then_mutate(fake_settings_profile_root, mutate)
    lines = brave_settings.build_export_lines(
        _args(fake_settings_profile_root), prefs_path, prefs
    )
    body = "\n".join(lines)
    doc = tomllib.loads(body)
    assert "weird" not in doc.get("settings", {})
    assert "not representable" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -q -k export_lines`
Expected: FAIL with `AttributeError: module 'dotbrave.settings' has no attribute 'build_export_lines'`.

- [ ] **Step 3: Implement build_export_lines**

In `src/dotbrave/_base/settings.py`, after `_is_volatile`, add:

```python
def build_export_lines(
    browser_name: str,
    args: argparse.Namespace,
    prefs_path: Path,
    prefs: dict,
) -> list[str] | None:
    """Build the ``[settings]`` block for `export`, or None without a snapshot.

    The block is the union of currently-managed keys (so applying the
    exported file does not reset them -- an absent managed key means
    "remove" to `apply`) and keys changed since the snapshot.  MAC-protected
    changes, removals, and non-TOML values are reported as comments.
    """
    snapshot = _load_snapshot(prefs_path)
    if snapshot is None:
        return None

    macs = _all_macs(prefs, prefs_path)
    lines = ["[settings]"]

    managed = sorted(_get_managed_keys(prefs_path))
    if managed:
        lines.append("# currently managed by dotbrave:")
        for key in managed:
            val = _get_value(prefs, _split_key(key))
            if val is _MISSING:
                lines.append(
                    f"# {json.dumps(key)} -- managed but not present in Preferences"
                )
                continue
            lines.append(f"{json.dumps(key)} = {_format_toml_value(val)}")
    managed_set = set(managed)

    changed: list[str] = []
    blocked: list[tuple[str, Any]] = []
    for parts, _old, new in _walk_leaf_diffs(snapshot["prefs"], prefs):
        if _is_volatile(parts):
            continue
        key = ".".join(parts)
        if key in managed_set:
            continue  # already emitted above, at its current value
        if _is_mac_protected(macs, parts):
            blocked.append((key, new))
            continue
        if new is _MISSING:
            changed.append(
                f"# {json.dumps(key)} was removed since the snapshot "
                "(apply cannot delete unmanaged keys)"
            )
            continue
        try:
            rhs = _format_toml_value(new)
        except ValueError:
            changed.append(
                f"# {json.dumps(key)} = {json.dumps(new)}  "
                "(value not representable in TOML)"
            )
            continue
        changed.append(f"{json.dumps(key)} = {rhs}")

    # Tracked prefs live in Secure Preferences; a diff there is a
    # MAC-protected setting changed via the UI.  protection.* churn is
    # already excluded by the volatile filter.
    secure_now = _load_secure_prefs(prefs_path)
    seen_blocked = {key for key, _ in blocked}
    for parts, _old, new in _walk_leaf_diffs(snapshot["secure_prefs"], secure_now):
        if _is_volatile(parts):
            continue
        key = ".".join(parts)
        if key not in seen_blocked:
            blocked.append((key, new))
            seen_blocked.add(key)

    lines.append(f"# changed since snapshot {snapshot['created']}:")
    if changed:
        lines.extend(changed)
    else:
        lines.append("# (no changes since snapshot)")

    if blocked:
        lines.append(
            "# changed since snapshot but MAC-protected -- `apply` would "
            f"refuse these; set them in the {browser_name.title()} UI:"
        )
        for key, new in blocked:
            if new is _MISSING:
                lines.append(f"#   {json.dumps(key)} (removed)")
                continue
            try:
                rhs = _format_toml_value(new)
            except ValueError:
                rhs = json.dumps(new)
            lines.append(f"#   {json.dumps(key)} = {rhs}")

    return lines
```

In `src/dotbrave/settings.py`, after `cmd_snapshot`, add:

```python
def build_export_lines(
    args: argparse.Namespace, prefs_path: Path, prefs: dict
) -> list[str] | None:
    return _base.build_export_lines("brave", args, prefs_path, prefs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_settings_snapshot.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/_base/settings.py src/dotbrave/settings.py tests/test_settings_snapshot.py
git commit -m "feat: build [settings] export block from snapshot diff"
```

---

### Task 4: Wire [settings] into `export` + header/help updates

**Files:**
- Modify: `src/dotbrave/browser.py` (`_export_settings` builder + `cmd_export` builders list, lines 289-300)
- Modify: `src/dotbrave/_base/orchestrator.py` (`_EXPORT_HEADER_NOTES` ~line 437; export subparser description ~line 624)
- Modify: `tests/test_export.py`

**Interfaces:**
- Consumes: `settings_mod.build_export_lines(args, prefs_path, prefs) -> list[str] | None` (Task 3).
- Produces: `dotbrave export` output containing a `[settings]` block between `[shortcuts]` and `[pwa]` whenever a snapshot exists; unchanged block set otherwise.

- [ ] **Step 1: Write the failing tests**

In `tests/test_export.py`:

1. In `test_brave_export_includes_diff_shortcuts_and_pwa`, replace the old header assertion:

```python
    # Header explains why settings is missing.
    assert "[settings] is intentionally NOT exported" in out
```

with:

```python
    # No snapshot in this fixture -> no [settings] block; header points
    # at the snapshot workflow instead.
    assert "dotbrave settings" in out
    assert "snapshot" in out
    assert "settings" not in tomllib.loads(out)
```

2. Update the module docstring's third sentence (`It deliberately omits [settings] ...`) to:

```python
`[settings]` is included only when a `settings snapshot` baseline exists;
without one the block is omitted.
```

3. Append two new tests at the end of the file:

```python
# ---------------------------------------------------------------------------
# Brave -- [settings] via snapshot diff
# ---------------------------------------------------------------------------

def test_brave_export_includes_settings_after_snapshot(
    fake_profile_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """snapshot -> mutate Preferences (as the Brave UI would) -> export
    emits a [settings] block with the changed key, ordered between
    [shortcuts] and [pwa]."""
    fake_policy = tmp_path / "policy" / "dotbrave-pwa.json"
    _redirect_pwa(monkeypatch, brave_pwa, fake_policy)
    _seed_policy(fake_policy, ["https://squoosh.app/"])

    from dotbrave import settings as brave_settings
    from dotbrave._base.utils import find_preferences, load_prefs

    args = argparse.Namespace(
        profile_root=fake_profile_root,
        profile="Default",
        output=None,
        all_shortcuts=False,
        channel="stable",
        clear=False,
    )
    brave_settings.cmd_snapshot(args)
    capsys.readouterr()  # discard snapshot chatter

    prefs_path = find_preferences(fake_profile_root, "Default")
    prefs = load_prefs(prefs_path)
    prefs.setdefault("brave", {}).setdefault("tabs", {})[
        "vertical_tabs_enabled"
    ] = True
    prefs_path.write_text(json.dumps(prefs))

    brave_pkg.cmd_export(args)
    out = capsys.readouterr().out

    doc = tomllib.loads(out)
    assert doc["settings"]["brave.tabs.vertical_tabs_enabled"] is True
    assert doc["shortcuts"] == {"focus_location": ["Alt+KeyL"]}
    assert doc["pwa"]["urls"] == ["https://squoosh.app/"]
    assert out.index("[shortcuts]") < out.index("[settings]") < out.index("[pwa]")

    # Round-trip: the exported [settings] table feeds plan_apply and
    # produces no value diff (values already match Preferences).
    plan = brave_settings.plan_apply(
        prefs_path, load_prefs(prefs_path), doc["settings"]
    )
    assert plan.diff_lines == []


def test_brave_export_no_settings_without_snapshot(
    fake_profile_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_policy = tmp_path / "policy" / "dotbrave-pwa.json"
    _redirect_pwa(monkeypatch, brave_pwa, fake_policy)

    args = argparse.Namespace(
        profile_root=fake_profile_root,
        profile="Default",
        output=None,
        all_shortcuts=False,
        channel="stable",
    )
    brave_pkg.cmd_export(args)
    out = capsys.readouterr().out
    assert "settings" not in tomllib.loads(out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src pytest tests/test_export.py -v`
Expected: the two new tests FAIL (`doc["settings"]` KeyError; header assertion fails because the old "intentionally NOT exported" text is still emitted).

- [ ] **Step 3: Implement wiring + header/help text**

In `src/dotbrave/browser.py`, between `_export_shortcuts` and `_export_pwa`, add:

```python
def _export_settings(
    args: argparse.Namespace, prefs_path: Path, prefs: dict
) -> list[str] | None:
    return settings_mod.build_export_lines(args, prefs_path, prefs)
```

and change `cmd_export`'s builders list to:

```python
        builders=[_export_shortcuts, _export_settings, _export_pwa],
```

In `src/dotbrave/_base/orchestrator.py`, replace the whole `_EXPORT_HEADER_NOTES` tuple with:

```python
_EXPORT_HEADER_NOTES = (
    "# This file captures user-visible customizations from your current",
    "# profile + managed-policy file:",
    "#",
    "#   [shortcuts] -- bindings that differ from Brave's defaults.",
    "#   [settings]  -- keys dotbrave already manages plus keys changed since",
    "#                  the last `dotbrave settings snapshot` (block omitted",
    "#                  when no snapshot exists).",
    "#   [pwa]       -- URLs currently force-installed via the managed-policy",
    "#                  file / Windows registry.",
    "#",
    "# To capture settings you change in the browser UI: run `dotbrave",
    "# settings snapshot`, change settings, then re-run `dotbrave export`.",
    "#",
    "# Apply this file with: `dotbrave apply <this file>`",
)
```

In `register_actions`, replace the `export_scope` assignment and the export parser `description` (currently the `"[settings] is intentionally not exported"` paragraph):

```python
        export_scope = (
            "[shortcuts] changes versus Brave defaults, [settings] keys "
            "managed by dotbrave plus keys changed since the last `settings "
            "snapshot`, and [pwa] force-installed URLs."
        )
        e = sub.add_parser(
            "export",
            help="emit exportable customizations as a TOML config",
            formatter_class=_HELP_FORMATTER,
            description=f"""\
Export a round-trippable TOML snapshot for {display_name}.

Output: {export_scope}

Chromium exposes no defaults table for arbitrary Preferences keys, so
[settings] is diffed against a baseline you capture with `settings
snapshot` before changing settings in the browser UI. Without a snapshot
the [settings] block is omitted. MAC-protected keys are emitted as
comments -- `apply` refuses them.""",
            epilog="""\
Examples:
  dotbrave export
  dotbrave export -o brave.toml""",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src pytest tests/test_export.py tests/test_settings_snapshot.py -q`
Expected: all PASS.

Run: `PYTHONPATH=src pytest -q`
Expected: all PASS (`tests/test_help.py` may assert export help text — if it fails, update its expected strings to the new description).

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/browser.py src/dotbrave/_base/orchestrator.py tests/test_export.py
git commit -m "feat: export [settings] as a snapshot diff in \`dotbrave export\`"
```

---

### Task 5: Documentation + full verification

**Files:**
- Modify: `README.md` (export row ~line 163, settings rows ~167-168, sidecar note ~line 191)
- Modify: `CLAUDE.md` (invariant 6)
- Test: full suite + `--help` smoke

**Interfaces:**
- Consumes: final CLI behavior from Tasks 1-4.
- Produces: docs consistent with runtime help (project rule: README + `--help` are the user-facing contract).

- [ ] **Step 1: Update README.md**

Replace the `export` action row (line 163) with:

```markdown
| `export [-o FILE] [-a]` | Emit `[shortcuts]` (only bindings that differ from Brave defaults; `-a/--all-shortcuts` lifts the filter), `[settings]` (keys managed by dotbrave plus keys changed since the last `settings snapshot`; omitted without a snapshot), and `[pwa]` as round-trippable TOML. |
```

After the `settings blocked` row (line 168), add:

```markdown
| `settings snapshot [--clear]` | Capture a Preferences baseline. Change settings in the Brave UI, then `export` emits the diff as `[settings]`. `--clear` deletes the baseline. Wait a few seconds after a UI change (Brave flushes Preferences on a delay) before exporting. |
```

Update the sidecar note at line 191-192 to mention the snapshot sidecar:

```markdown
`[shortcuts]` and `[settings]` track managed entries in sidecar files
(`Preferences.dotbrave.{shortcuts,settings}.json`), so removing a key from
```
→ append after that sentence's paragraph: `` `settings snapshot` stores its baseline in a third sidecar (`Preferences.dotbrave.settings-snapshot.json`); `restore` leaves it alone. ``

- [ ] **Step 2: Update CLAUDE.md invariant 6**

Replace:

```markdown
6. `export` intentionally omits `[settings]` (Chromium has no defaults
   table for arbitrary prefs). It emits `[shortcuts]` diffs against
   `brave.default_accelerators` plus `[pwa]`.
```

with:

```markdown
6. `export` emits `[shortcuts]` diffs against `brave.default_accelerators`,
   `[pwa]`, and -- only when a `settings snapshot` baseline sidecar exists --
   a `[settings]` block: currently-managed keys (so re-applying the export
   cannot reset them) plus keys changed since the snapshot, with
   MAC-protected changes as comments. There is no defaults table for
   arbitrary prefs, so no snapshot means no `[settings]` block. `export`
   never consumes the snapshot; `restore` does not delete it.
```

- [ ] **Step 3: Full verification**

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m dotbrave --help
PYTHONPATH=src python -m dotbrave export --help
PYTHONPATH=src python -m dotbrave settings snapshot --help
```

Expected: suite passes; help output shows the snapshot workflow, no traceback.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document snapshot-based [settings] export"
```
