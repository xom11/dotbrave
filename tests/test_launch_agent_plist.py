"""The shipped LaunchAgent must give Brave a *dynamic* debugging port.

Brave writes DevToolsActivePort only for --remote-debugging-port=0; started
on a fixed port it writes nothing and dotbrave's file-based discovery sees
no endpoint, which is the exact restart this plist exists to remove.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parents[1] / "contrib" / "org.dotbrave.brave-endpoint.plist"


def _load() -> dict:
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_parses_and_is_labelled() -> None:
    assert PLIST.exists(), f"missing {PLIST}"
    data = _load()
    assert data["Label"] == "org.dotbrave.brave-endpoint"


def test_plist_starts_brave_with_a_dynamic_debugging_port() -> None:
    args = _load()["ProgramArguments"]
    assert args[0].endswith("/Contents/MacOS/Brave Browser")
    assert "--remote-debugging-port=0" in args
    assert not any(
        a.startswith("--remote-debugging-port=") and a != "--remote-debugging-port=0"
        for a in args
    )
    assert "--remote-debugging-pipe" not in args


def test_plist_runs_at_login_and_does_not_resurrect_a_quit_browser() -> None:
    data = _load()
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False
