"""Shared pytest fixtures for dotbrave tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def fake_settings_profile_root(tmp_path: Path) -> Path:
    """Profile root for settings tests.

    The synthesized `Preferences` includes one MAC-protected key
    (`browser.show_home_button` mirrored under `protection.macs.*`) so
    the refusal path can be exercised, plus several plain keys with
    different value types (bool, string, list) for the apply round-trip.
    """
    profile = tmp_path / "Default"
    profile.mkdir()
    prefs = {
        "brave": {
            "tabs": {
                "vertical_tabs_enabled": False,
                "vertical_tabs_collapsed": True,
            },
        },
        "bookmark_bar": {
            "show_tab_groups": False,
        },
        "browser": {
            "show_home_button": True,  # Mirror is in protection.macs below
        },
        "homepage": "https://existing-home.example",
        "protection": {
            "macs": {
                "browser": {
                    "show_home_button": "DEADBEEF" * 8,
                },
                "homepage": "CAFEBABE" * 8,
            }
        },
        "some": {"unrelated": "preference"},
    }
    (profile / "Preferences").write_text(json.dumps(prefs))
    return tmp_path


@pytest.fixture
def fake_profile_root(tmp_path: Path) -> Path:
    """A directory that mimics Brave's profile-root layout: a `Default`
    sub-folder containing a minimal `Preferences` JSON.

    The synthesized `brave.default_accelerators` includes a few real
    Chromium command IDs so that the reset-to-default code path has
    something to reset to.
    """
    from dotbrave.command_ids import NAME_TO_ID

    profile = tmp_path / "Default"
    profile.mkdir()
    prefs = {
        "brave": {
            # Pre-existing user customization (so `dump` has something to emit).
            # focus_location is already overridden; new_tab is at default.
            "accelerators": {
                str(NAME_TO_ID["focus_location"]): ["Alt+KeyL"],
                str(NAME_TO_ID["new_tab"]): ["Control+KeyT"],
            },
            "default_accelerators": {
                str(NAME_TO_ID["focus_location"]): ["Control+KeyL", "Alt+KeyD"],
                str(NAME_TO_ID["new_tab"]): ["Control+KeyT"],
                str(NAME_TO_ID["toggle_sidebar"]): ["Control+Shift+KeyS"],
            },
        },
        "some": {"unrelated": "preference"},
    }
    (profile / "Preferences").write_text(json.dumps(prefs))
    return tmp_path
