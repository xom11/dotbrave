"""Tests specific to the unified `brave apply` orchestration.

The other apply test files cover one namespace each via the same
unified entry point. This file focuses on the cross-module concerns:
combined diff, single-cycle write, and missing-vs-empty-table semantics.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave import pwa as pwa_mod

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def combined_profile_root(tmp_path: Path) -> Path:
    """Profile that has both shortcuts defaults (for the shortcuts side)
    AND a MAC entry (so the parent-of-protected refusal can also be
    exercised in cross-module cases). No tracked-pref refusal in the
    happy paths so we can write a combined config end-to-end."""
    from dotbrave.command_ids import NAME_TO_ID

    profile = tmp_path / "Default"
    profile.mkdir()
    prefs = {
        "brave": {
            "accelerators": {
                str(NAME_TO_ID["focus_location"]): ["Alt+KeyL"],
            },
            "default_accelerators": {
                str(NAME_TO_ID["focus_location"]): ["Control+KeyL"],
                str(NAME_TO_ID["new_tab"]): ["Control+KeyT"],
            },
            "tabs": {
                "vertical_tabs_enabled": False,
            },
        },
        "bookmark_bar": {"show_tab_groups": False},
        "protection": {
            "macs": {"browser": {"show_home_button": "DEAD" * 16}}
        },
    }
    (profile / "Preferences").write_text(json.dumps(prefs))
    return tmp_path


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


def _apply(profile_root: Path, config: Path) -> None:
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=config,
        dry_run=False,
    )
    brave_pkg.cmd_apply(args)


def test_combined_apply_writes_both_namespaces_in_one_cycle(
    combined_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A single TOML with both [shortcuts] and [settings] should produce
    one backup, one Preferences write, and two state files."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
        '"bookmark_bar.show_tab_groups" = true\n'
    )

    _apply(combined_profile_root, cfg)

    p = json.loads((combined_profile_root / "Default" / "Preferences").read_text())

    from dotbrave.command_ids import NAME_TO_ID
    # shortcuts side
    assert p["brave"]["accelerators"][str(NAME_TO_ID["focus_location"])] == ["Alt+KeyD"]
    # settings side
    assert p["brave"]["tabs"]["vertical_tabs_enabled"] is True
    assert p["bookmark_bar"]["show_tab_groups"] is True

    # Both sidecar state files should exist with the right entries
    sc_state = json.loads(
        (combined_profile_root / "Default" / "Preferences.dotbrave.shortcuts.json").read_text()
    )
    st_state = json.loads(
        (combined_profile_root / "Default" / "Preferences.dotbrave.settings.json").read_text()
    )
    assert sc_state["managed_ids"] == [str(NAME_TO_ID["focus_location"])]
    assert sorted(st_state["managed_keys"]) == [
        "bookmark_bar.show_tab_groups",
        "brave.tabs.vertical_tabs_enabled",
    ]

    # Exactly one timestamped backup — a combined apply should not produce
    # one backup per module.
    backups = list((combined_profile_root / "Default").glob("Preferences.bak.*"))
    assert len(backups) == 1, f"expected exactly one backup, got {backups}"


def test_combined_dry_run_shows_grouped_diff(
    combined_profile_root: Path, tmp_path: Path
) -> None:
    """Dry-run output should label each section by namespace so the user
    can tell which module is doing what."""
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
    )

    r = _run_cli(combined_profile_root, "apply", str(cfg), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "shortcuts:" in r.stdout
    assert "settings:" in r.stdout
    assert "(dry-run, nothing written)" in r.stdout


def test_settings_refusal_blocks_shortcuts_write_too(
    combined_profile_root: Path, tmp_path: Path
) -> None:
    """If [settings] contains a MAC-protected key, the WHOLE apply must
    fail — shortcuts must not be written either, since we promise a
    single atomic cycle. This is the "no partial write" guarantee."""
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"browser.show_home_button" = false\n'
    )

    before = (combined_profile_root / "Default" / "Preferences").read_bytes()
    r = _run_cli(combined_profile_root, "apply", str(cfg))
    assert r.returncode != 0
    assert "MAC-protected" in (r.stdout + r.stderr)

    after = (combined_profile_root / "Default" / "Preferences").read_bytes()
    assert before == after, "shortcuts must not be written when settings refuses"

    # No state file should have been created either
    assert not (combined_profile_root / "Default" / "Preferences.dotbrave.shortcuts.json").exists()
    assert not (combined_profile_root / "Default" / "Preferences.dotbrave.settings.json").exists()


def test_missing_table_skips_module_entirely(
    combined_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A TOML without [settings] must not touch settings state, even if
    a previous run left a state file behind."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    # First run: apply both namespaces, populating both state files.
    full = tmp_path / "full.toml"
    full.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
    )
    _apply(combined_profile_root, full)

    settings_state_path = (
        combined_profile_root / "Default" / "Preferences.dotbrave.settings.json"
    )
    settings_state_before = settings_state_path.read_text()

    p_before = json.loads((combined_profile_root / "Default" / "Preferences").read_text())
    settings_value_before = p_before["brave"]["tabs"]["vertical_tabs_enabled"]

    # Second run: only [shortcuts] in the TOML. The settings module must
    # be skipped entirely — state file unchanged, pref value unchanged.
    only_sc = tmp_path / "only-sc.toml"
    only_sc.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyZ"]\n'
    )
    _apply(combined_profile_root, only_sc)

    assert settings_state_path.read_text() == settings_state_before
    p_after = json.loads((combined_profile_root / "Default" / "Preferences").read_text())
    assert p_after["brave"]["tabs"]["vertical_tabs_enabled"] == settings_value_before


def test_three_namespace_apply_in_one_cycle(
    combined_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """[shortcuts] + [settings] + [pwa] in one TOML must all apply in a
    single backup + write cycle. pwa's external (sudo) write runs
    BEFORE Preferences are committed so a sudo failure leaves
    Preferences unchanged (see test_external_failure_leaves_prefs_unchanged
    below for the failure-mode assertion).
    """
    if not (sys.platform.startswith("linux") or sys.platform == "darwin" or sys.platform == "win32"):
        pytest.skip("pwa apply path is implemented for Linux, macOS and Windows")

    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    # Redirect pwa's policy storage and neutralize elevation (mirroring the
    # fake_policy fixture in test_pwa_apply.py — duplicated rather than
    # extracted because pulling it into a conftest would force the
    # other tests to depend on a pwa-shaped fixture).
    if sys.platform == "darwin":
        fake_policy = tmp_path / "policy" / "com.brave.Browser.plist"
    else:
        fake_policy = tmp_path / "policy" / "dotbrave-pwa.json"

    if sys.platform == "win32":
        import ctypes
        import json as json_mod

        def fake_read_payload() -> dict:
            if not fake_policy.exists():
                return {}
            try:
                return json_mod.loads(fake_policy.read_text())
            except (json_mod.JSONDecodeError, OSError):
                return {}

        monkeypatch.setattr(pwa_mod, "_read_existing_payload", fake_read_payload)

        def fake_sudo_write(entries):
            fake_policy.parent.mkdir(parents=True, exist_ok=True)
            payload = {pwa_mod.POLICY_KEY: entries}
            fake_policy.write_text(json_mod.dumps(payload, indent=2))

        monkeypatch.setattr(pwa_mod, "_sudo_write_policy", fake_sudo_write)
        monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    else:
        monkeypatch.setattr(pwa_mod, "POLICY_FILE", fake_policy)

        def fake_sudo_write(entries):
            fake_policy.parent.mkdir(parents=True, exist_ok=True)
            fake_policy.write_bytes(pwa_mod._build_policy_payload(entries))

        monkeypatch.setattr(pwa_mod, "_sudo_write_policy", fake_sudo_write)

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if list(cmd[:3]) == ["sudo", "-n", "true"]:
                return subprocess.CompletedProcess(cmd, 0)
            if list(cmd[:2]) == ["sudo", "-v"]:
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        from dotbrave._base import orchestrator as orch
        monkeypatch.setattr(orch.subprocess, "run", fake_run)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
        '\n'
        '[pwa]\n'
        'urls = ["https://squoosh.app/"]\n'
    )
    _apply(combined_profile_root, cfg)

    # All three namespaces landed
    p = json.loads((combined_profile_root / "Default" / "Preferences").read_text())
    from dotbrave.command_ids import NAME_TO_ID

    assert p["brave"]["accelerators"][str(NAME_TO_ID["focus_location"])] == ["Alt+KeyD"]
    assert p["brave"]["tabs"]["vertical_tabs_enabled"] is True
    if sys.platform == "darwin":
        import plistlib
        with fake_policy.open("rb") as f:
            pol = plistlib.load(f)
    else:
        pol = json.loads(fake_policy.read_text())
    assert [e["url"] for e in pol[pwa_mod.POLICY_KEY]] == ["https://squoosh.app/"]

    # Single backup despite three plans — that's the unified-cycle promise.
    backups = list((combined_profile_root / "Default").glob("Preferences.bak.*"))
    assert len(backups) == 1


def test_empty_table_resets_managed_entries(
    combined_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """An EMPTY [settings] header (different from missing) is the explicit
    "wipe all my managed settings" gesture — managed keys are popped from
    Preferences and the state file becomes empty."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    # Seed: apply something
    seed = tmp_path / "seed.toml"
    seed.write_text(
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
        '"bookmark_bar.show_tab_groups" = true\n'
    )
    _apply(combined_profile_root, seed)
    p = json.loads((combined_profile_root / "Default" / "Preferences").read_text())
    assert p["brave"]["tabs"]["vertical_tabs_enabled"] is True

    # Now apply with [settings] header but no body
    empty = tmp_path / "empty.toml"
    empty.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
    )
    _apply(combined_profile_root, empty)

    p = json.loads((combined_profile_root / "Default" / "Preferences").read_text())
    # Both settings managed by the seed should be popped
    assert "vertical_tabs_enabled" not in p["brave"].get("tabs", {})
    assert "show_tab_groups" not in p.get("bookmark_bar", {})

    state = json.loads(
        (combined_profile_root / "Default" / "Preferences.dotbrave.settings.json").read_text()
    )
    assert state["managed_keys"] == []


def test_external_failure_leaves_prefs_unchanged(
    combined_profile_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """If a [pwa] external_apply_fn raises (sudo flake, EACCES on a
    network mount, ...), Preferences must NOT be committed -- the
    shortcuts/settings changes should roll forward together with the
    pwa write or not at all.

    Implementation guarantee: orchestrator runs external_apply_fn
    BEFORE write_atomic. This test pins that ordering.
    """
    if not (sys.platform.startswith("linux") or sys.platform == "darwin" or sys.platform == "win32"):
        pytest.skip("pwa apply path is implemented for Linux, macOS and Windows")

    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    # Stub the privilege preflight + read path; only the *write* should fail.
    if sys.platform == "win32":
        import ctypes
        monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
        monkeypatch.setattr(pwa_mod, "_read_existing_payload", lambda: {})
    else:
        if sys.platform == "darwin":
            fake_policy = tmp_path / "policy" / "com.brave.Browser.plist"
        else:
            fake_policy = tmp_path / "policy" / "dotbrave-pwa.json"
        monkeypatch.setattr(pwa_mod, "POLICY_FILE", fake_policy)

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if list(cmd[:3]) == ["sudo", "-n", "true"]:
                return subprocess.CompletedProcess(cmd, 0)
            if list(cmd[:2]) == ["sudo", "-v"]:
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        from dotbrave._base import orchestrator as orch
        monkeypatch.setattr(orch.subprocess, "run", fake_run)

    def boom(_entries):
        raise SystemExit("simulated sudo failure")

    monkeypatch.setattr(pwa_mod, "_sudo_write_policy", boom)

    prefs_path = combined_profile_root / "Default" / "Preferences"
    before = prefs_path.read_bytes()

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[shortcuts]\n'
        'focus_location = ["Alt+KeyD"]\n'
        '\n'
        '[settings]\n'
        '"brave.tabs.vertical_tabs_enabled" = true\n'
        '\n'
        '[pwa]\n'
        'urls = ["https://squoosh.app/"]\n'
    )

    with pytest.raises(SystemExit, match="simulated sudo failure"):
        _apply(combined_profile_root, cfg)

    # The contract: Preferences was not touched.
    assert prefs_path.read_bytes() == before, (
        "external_apply_fn failure must leave Preferences unchanged"
    )
    # State sidecars must not exist either -- they're written after
    # write_atomic, which we never reached.
    assert not (combined_profile_root / "Default" / "Preferences.dotbrave.shortcuts.json").exists()
    assert not (combined_profile_root / "Default" / "Preferences.dotbrave.settings.json").exists()
