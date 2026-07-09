"""Pure-logic tests — no Brave process, no real profile, no subprocess."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotbrave import settings as st
from dotbrave import shortcuts as sc
from dotbrave import utils as ut


# ---------------------------------------------------------------------------
# shortcuts._validate_table  (was load_config; TOML loading now happens at
# the unified-apply level, modules just validate the parsed table)
# ---------------------------------------------------------------------------

def test_validate_table_valid() -> None:
    raw = {
        "focus_location": ["Control+KeyL", "Alt+KeyD"],
        "new_tab": ["Control+KeyT"],
    }
    assert sc._validate_table(raw) == raw


def test_validate_table_rejects_non_dict() -> None:
    with pytest.raises(SystemExit, match=r"\[shortcuts\] must be a table"):
        sc._validate_table([])


def test_validate_table_rejects_non_list_value() -> None:
    with pytest.raises(SystemExit, match="must be a list of strings"):
        sc._validate_table({"focus_location": "Control+KeyL"})


def test_validate_table_rejects_non_string_in_list() -> None:
    with pytest.raises(SystemExit, match="must be a list of strings"):
        sc._validate_table({"focus_location": ["Control+KeyL", 42]})


# ---------------------------------------------------------------------------
# shortcuts._normalize_keys — platform-specific modifier rewrite
#
# Brave uses `Command+` on macOS and `Meta+` on Linux/Windows for the
# super/cmd key. The wrong spelling is silently dropped at parse time
# (e.g. `Meta+KeyR` on macOS → bare `KeyR`), which is destructive.
# ---------------------------------------------------------------------------

import sys as _sys


def test_normalize_meta_to_command_on_macos(monkeypatch) -> None:
    monkeypatch.setattr(_sys, "platform", "darwin")
    assert sc._normalize_keys(["Meta+KeyR", "Control+KeyT"]) == [
        "Command+KeyR",
        "Control+KeyT",
    ]


def test_normalize_command_to_meta_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(_sys, "platform", "linux")
    assert sc._normalize_keys(["Command+KeyR", "Control+KeyT"]) == [
        "Meta+KeyR",
        "Control+KeyT",
    ]


def test_normalize_dedupes_after_translation(monkeypatch) -> None:
    """Both spellings collapse on the target platform — user intent is
    "the super/cmd key" either way; no point keeping a duplicate."""
    monkeypatch.setattr(_sys, "platform", "darwin")
    assert sc._normalize_keys(["Meta+KeyT", "Command+KeyT"]) == ["Command+KeyT"]


def test_normalize_leaves_other_modifiers_alone(monkeypatch) -> None:
    monkeypatch.setattr(_sys, "platform", "darwin")
    assert sc._normalize_keys(["Control+Shift+KeyE", "Alt+KeyA"]) == [
        "Control+Shift+KeyE",
        "Alt+KeyA",
    ]


# ---------------------------------------------------------------------------
# resolve_command_ids
# ---------------------------------------------------------------------------

def test_resolve_known_names() -> None:
    from dotbrave.command_ids import NAME_TO_ID

    out = sc.resolve_command_ids({"focus_location": ["Control+KeyL"]})
    # CLI stores command IDs stringified (matches Brave's Preferences format)
    assert out == {str(NAME_TO_ID["focus_location"]): ["Control+KeyL"]}


def test_resolve_unknown_name_exits() -> None:
    with pytest.raises(SystemExit, match="unknown command name"):
        sc.resolve_command_ids({"definitely_not_a_real_command": ["F13"]})


# ---------------------------------------------------------------------------
# diff_summary (shortcuts)
# ---------------------------------------------------------------------------

def test_diff_summary_added_modified_removed() -> None:
    from dotbrave.command_ids import NAME_TO_ID

    focus = str(NAME_TO_ID["focus_location"])
    new_tab = str(NAME_TO_ID["new_tab"])
    unknown_id = "999999"  # outside the mapping → renders as <unknown:...>

    current = {focus: ["Control+KeyL"], new_tab: ["Control+KeyT"]}
    target = {
        focus: ["Alt+KeyD"],            # modified
        unknown_id: ["F12"],             # added (unknown id falls through)
    }
    removed = {new_tab}                  # removed (was managed, now gone)
    lines = sc.diff_summary(current, target, removed)
    joined = "\n".join(lines)
    assert "~ focus_location" in joined
    assert "+ <unknown:" + unknown_id + ">" in joined
    assert "- new_tab" in joined and "reset to default" in joined


def test_diff_summary_no_changes_returns_empty() -> None:
    current = {"35012": ["Control+KeyL"]}
    target = {"35012": ["Control+KeyL"]}
    assert sc.diff_summary(current, target, set()) == []


# ---------------------------------------------------------------------------
# write_atomic (lives in utils now)
# ---------------------------------------------------------------------------

def test_write_atomic_replaces_file(tmp_path: Path) -> None:
    p = tmp_path / "Preferences"
    p.write_text(json.dumps({"old": True}))
    ut.write_atomic(p, {"new": True})
    assert json.loads(p.read_text()) == {"new": True}
    # No leftover .tmp file
    assert not p.with_suffix(p.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# shortcuts state file (managed_ids sidecar)
#
# After the unified-apply refactor there is no public _set_managed_ids:
# state writing happens via Plan.state_payload → Plan.state_path. The
# round-trip test below mirrors that flow exactly.
# ---------------------------------------------------------------------------

def test_managed_ids_round_trip(tmp_path: Path) -> None:
    prefs = tmp_path / "Preferences"
    prefs.write_text("{}")
    assert sc._get_managed_ids(prefs) == set()

    sidecar = prefs.with_name(prefs.name + ".dotbrave.shortcuts.json")
    sidecar.write_text(json.dumps({"managed_ids": sorted(["35012", "34014"], key=int)}))
    assert sc._get_managed_ids(prefs) == {"35012", "34014"}

    data = json.loads(sidecar.read_text())
    # IDs are sorted numerically, not lexicographically
    assert data["managed_ids"] == ["34014", "35012"]


def test_managed_ids_handles_corrupt_sidecar(tmp_path: Path) -> None:
    prefs = tmp_path / "Preferences"
    sidecar = prefs.with_name(prefs.name + ".dotbrave.shortcuts.json")
    sidecar.write_text("not json {{")
    assert sc._get_managed_ids(prefs) == set()


# ---------------------------------------------------------------------------
# find_preferences (shortcuts re-exports it from utils)
# ---------------------------------------------------------------------------

def test_find_preferences_ok(fake_profile_root: Path) -> None:
    p = sc.find_preferences(fake_profile_root, "Default")
    assert p == fake_profile_root / "Default" / "Preferences"


def test_find_preferences_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Preferences not found"):
        sc.find_preferences(tmp_path, "NoSuchProfile")


# ---------------------------------------------------------------------------
# get_nested
# ---------------------------------------------------------------------------

def test_get_nested_creates_intermediate_dicts() -> None:
    d: dict = {}
    inner = sc.get_nested(d, ("a", "b", "c"))
    inner["x"] = 1
    assert d == {"a": {"b": {"c": {"x": 1}}}}


# ---------------------------------------------------------------------------
# settings._is_mac_protected — load-bearing for v1 refusal
# ---------------------------------------------------------------------------

def test_is_mac_protected_exact_match() -> None:
    macs = {"browser": {"show_home_button": "DEAD"}}
    assert st._is_mac_protected(macs, ("browser", "show_home_button")) is True


def test_is_mac_protected_parent_of_tracked_leaf() -> None:
    """Writing the parent dict would clobber a tracked child, so we refuse it."""
    macs = {"browser": {"show_home_button": "DEAD"}}
    assert st._is_mac_protected(macs, ("browser",)) is True


def test_is_mac_protected_not_in_tree() -> None:
    macs = {"browser": {"show_home_button": "DEAD"}}
    assert st._is_mac_protected(macs, ("brave", "tabs", "vertical_tabs_enabled")) is False


def test_is_mac_protected_no_protection_subtree() -> None:
    """A profile that has no protection.macs at all → nothing is tracked."""
    assert st._is_mac_protected({}, ("homepage",)) is False
