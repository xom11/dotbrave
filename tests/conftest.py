"""Shared pytest fixtures for dotbrave tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_launcher_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every [pwa] apply away from real OS launcher directories.
    Tests of the heal or cleanup re-patch ``pwa._launcher_heal`` /
    ``pwa._launcher_cleanup``."""
    from dotbrave import pwa

    monkeypatch.setattr(pwa, "_launcher_heal", lambda: None)
    monkeypatch.setattr(pwa, "_launcher_cleanup", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _desktop_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if from a terminal inside the desktop session.

    CI runners (and an agent's shell) have no display, so the Linux
    relaunch guard would refuse every close a test drives.  Tests of the
    guard delete these again.  The post-restart grace and the wait for a
    previous instance to exit are skipped too: the process callbacks here
    are fakes that answer instantly."""
    from dotbrave._base import orchestrator

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setattr(orchestrator, "_REOPEN_GRACE_S", 0.0)
    monkeypatch.setattr(orchestrator, "_PREVIOUS_EXIT_TIMEOUT_S", 0.0)


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
