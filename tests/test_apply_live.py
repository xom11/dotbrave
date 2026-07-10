"""End-to-end test for the unified `brave apply` against a synthesized profile.

Builds a fake `Default/Preferences` in a tmp dir and exercises the full
apply/reset round-trip via the unified entry point in
`dotbrave.browser.cmd_apply`. The TOML file used here only carries a
`[shortcuts]` table, so the settings module is a no-op (missing-table
= skip). To stay independent of the user's running Brave session, the
in-process apply path runs with `brave_running` monkeypatched to False;
only the dry-run + error-path checks go through the CLI subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotbrave import browser as brave_pkg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(profile_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dotbrave",
            "--profile-root",
            str(profile_root),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _write_config(path: Path, mapping: dict[str, list[str]]) -> None:
    lines = ["[shortcuts]"]
    for name, keys in mapping.items():
        keys_repr = "[" + ", ".join(json.dumps(k) for k in keys) + "]"
        lines.append(f"{name} = {keys_repr}")
    path.write_text("\n".join(lines) + "\n")


def _accelerators(profile_root: Path) -> dict[str, list[str]]:
    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    return prefs.get("brave", {}).get("accelerators", {})


def _apply(profile_root: Path, config: Path) -> None:
    """Drive the unified cmd_apply in-process with a fake Namespace."""
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=config,
        dry_run=False,
    )
    brave_pkg.cmd_apply(args)


def test_apply_writes_then_resets(
    fake_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """Full round-trip: apply → re-apply (no-op) → drop entry → reset."""
    from dotbrave.command_ids import NAME_TO_ID

    # Stub the Brave-is-running check; we're operating on a tmp profile
    # that has nothing to do with the real running Brave instance.
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "shortcuts.toml"

    # --- 1. dry-run via the real CLI: nothing written ------------------
    _write_config(cfg, {"focus_location": ["Alt+KeyD"]})
    before = (fake_profile_root / "Default" / "Preferences").read_bytes()
    r = _run_cli(fake_profile_root, "apply", str(cfg), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "(dry-run, nothing written)" in r.stdout
    after = (fake_profile_root / "Default" / "Preferences").read_bytes()
    assert before == after, "dry-run must not modify Preferences"

    # --- 2. real apply (in-process): bindings land in accelerators -----
    _write_config(
        cfg,
        {
            "focus_location": ["Alt+KeyD"],
            "new_tab": ["Control+KeyT", "Control+Shift+KeyT"],
            "toggle_sidebar": ["Control+Shift+KeyE"],
        },
    )
    _apply(fake_profile_root, cfg)

    accels = _accelerators(fake_profile_root)
    assert accels[str(NAME_TO_ID["focus_location"])] == ["Alt+KeyD"]
    assert accels[str(NAME_TO_ID["new_tab"])] == [
        "Control+KeyT",
        "Control+Shift+KeyT",
    ]
    assert accels[str(NAME_TO_ID["toggle_sidebar"])] == ["Control+Shift+KeyE"]

    # Sidecar state file should track exactly the IDs we wrote
    sidecar = (
        fake_profile_root
        / "Default"
        / "Preferences.dotbrave.shortcuts.json"
    )
    state = json.loads(sidecar.read_text())
    assert sorted(state["managed_ids"]) == sorted(
        [
            str(NAME_TO_ID["focus_location"]),
            str(NAME_TO_ID["new_tab"]),
            str(NAME_TO_ID["toggle_sidebar"]),
        ]
    )

    # A timestamped backup should exist
    backups = list((fake_profile_root / "Default").glob("Preferences.bak.*"))
    assert backups, "expected at least one backup file"

    # --- 3. apply again with no change → "no changes" -------------------
    _apply(fake_profile_root, cfg)  # idempotency

    # --- 4. drop two entries → they should reset to default ------------
    _write_config(cfg, {"focus_location": ["Alt+KeyD"]})  # others removed
    _apply(fake_profile_root, cfg)
    accels = _accelerators(fake_profile_root)
    # focus_location still has the user override
    assert accels[str(NAME_TO_ID["focus_location"])] == ["Alt+KeyD"]
    # new_tab reset to whatever brave.default_accelerators said
    assert accels[str(NAME_TO_ID["new_tab"])] == ["Control+KeyT"]
    assert accels[str(NAME_TO_ID["toggle_sidebar"])] == ["Control+Shift+KeyS"]

    state = json.loads(sidecar.read_text())
    assert state["managed_ids"] == [str(NAME_TO_ID["focus_location"])]


def test_apply_unknown_command_errors(fake_profile_root: Path, tmp_path: Path) -> None:
    """Unknown command names must fail loudly before touching the file."""
    cfg = tmp_path / "bad.toml"
    cfg.write_text('[shortcuts]\nnot_a_real_command = ["F13"]\n')

    before = (fake_profile_root / "Default" / "Preferences").read_bytes()
    r = _run_cli(fake_profile_root, "apply", str(cfg))
    assert r.returncode != 0
    assert "unknown command name" in (r.stdout + r.stderr)
    after = (fake_profile_root / "Default" / "Preferences").read_bytes()
    assert before == after, "failed apply must not modify Preferences"


def test_dump_and_list_against_fake_profile(fake_profile_root: Path) -> None:
    """`export` reads the fake profile's accelerators; `-a` lifts the
    user-overridden-only filter (shortcut-name discovery path)."""
    # Plain export only emits user-overridden entries; the fixture has
    # focus_location overridden but new_tab matching default.
    r = _run_cli(fake_profile_root, "export")
    assert r.returncode == 0
    assert "focus_location" in r.stdout
    # new_tab matches default in the fixture, so it should NOT appear
    assert "new_tab" not in r.stdout

    # `export -a` should include every binding in accelerators
    r = _run_cli(fake_profile_root, "export", "-a")
    assert r.returncode == 0
    assert "focus_location" in r.stdout
    assert "new_tab" in r.stdout


def test_apply_empty_config_errors(fake_profile_root: Path, tmp_path: Path) -> None:
    """A TOML with no recognized table is a usage error — refuse rather
    than silently doing nothing, since the user almost certainly meant
    to put one in."""
    cfg = tmp_path / "empty.toml"
    cfg.write_text("# nothing here\n")

    r = _run_cli(fake_profile_root, "apply", str(cfg))
    assert r.returncode != 0
    assert "no [shortcuts], [settings] or [pwa]" in (r.stdout + r.stderr)
