"""End-to-end tests for `dotbrave apply` exercising the [shields] table.

Same isolation as tests/test_pwa_apply.py: the policy storage is
redirected into tmp_path, and the privileged writer is replaced by a
direct write that still goes through the real payload builder, so the
platform serialization (and the macOS merge with [pwa]) runs for real.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave import pwa, shields
from dotbrave._base import pwa as base_pwa

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the Windows path is the registry; covered by the shared writer",
)


def _read(path: Path) -> dict:
    if sys.platform == "darwin":
        with path.open("rb") as f:
            return plistlib.load(f)
    return json.loads(path.read_text())


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"some": "thing"}))
    return tmp_path


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Both namespaces' storage in tmp. On macOS they share one plist, as
    they do for real; on Linux each has its own file."""
    pdir = tmp_path / "policy"
    if sys.platform == "darwin":
        pwa_file = shields_file = pdir / "com.brave.Browser.plist"
    else:
        pwa_file = pdir / "dotbrave-pwa.json"
        shields_file = pdir / "dotbrave-shields.json"
    monkeypatch.setattr(pwa, "POLICY_FILE", pwa_file)
    monkeypatch.setattr(shields, "POLICY_FILE", shields_file)
    writes: list[str] = []

    def write_pwa(entries: list[dict]) -> None:
        writes.append("pwa")
        pwa_file.parent.mkdir(parents=True, exist_ok=True)
        pwa_file.write_bytes(pwa._build_policy_payload(entries))

    def write_shields(patterns: list[str]) -> None:
        writes.append("shields")
        shields_file.parent.mkdir(parents=True, exist_ok=True)
        shields_file.write_bytes(base_pwa.build_policy_payload(
            shields_file, "", patterns, shields.POLICY_KEY,
        ))

    monkeypatch.setattr(pwa, "_sudo_write_policy", write_pwa)
    monkeypatch.setattr(shields, "_sudo_write_policy", write_shields)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if list(cmd[:3]) == ["sudo", "-n", "true"] or list(cmd[:2]) == ["sudo", "-v"]:
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *args, **kwargs)

    from dotbrave._base import orchestrator as orch
    monkeypatch.setattr(orch.subprocess, "run", fake_run)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    return {"pwa": pwa_file, "shields": shields_file, "writes": writes}


def _apply(profile_root: Path, config: Path, **extra) -> None:
    args = argparse.Namespace(
        profile_root=profile_root, profile="Default", config=config,
        dry_run=False, **extra,
    )
    brave_pkg.cmd_apply(args)


def _config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "brave.toml"
    cfg.write_text(body)
    return cfg


# ---------------------------------------------------------------------------
# validate_table
# ---------------------------------------------------------------------------


def test_validate_dedupes_and_keeps_order() -> None:
    raw = {"disabled": ["[*.]b.com", "[*.]a.com", "[*.]b.com"]}
    assert shields.validate_table(raw) == ["[*.]b.com", "[*.]a.com"]


def test_validate_empty_table_is_wipe() -> None:
    assert shields.validate_table({}) == []


@pytest.mark.parametrize("raw, needle", [
    ([], "must be a table"),
    ({"enabled": []}, "unsupported keys"),
    ({"disabled": "[*.]a.com"}, "must be an array"),
    ({"disabled": [1]}, "non-empty strings"),
    ({"disabled": [""]}, "non-empty strings"),
    ({"disabled": [" [*.]a.com"]}, "surrounding whitespace"),
])
def test_validate_rejects(raw: object, needle: str) -> None:
    with pytest.raises(SystemExit) as e:
        shields.validate_table(raw)
    assert needle in str(e.value)


def test_pwa_managed_keys_name_the_shields_policy() -> None:
    """The macOS daemon decision reads this tuple; a shields key missing
    from it means emptying [pwa] removes the daemon under [shields]."""
    assert shields.POLICY_KEY in pwa.MANAGED_POLICY_KEYS


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_first_apply_writes_policy(
    profile_root: Path, policy: dict, tmp_path: Path, capsys,
) -> None:
    cfg = _config(tmp_path, '[shields]\ndisabled = ["[*.]messenger.com"]\n')
    _apply(profile_root, cfg)

    assert _read(policy["shields"])[shields.POLICY_KEY] == ["[*.]messenger.com"]
    assert "+ [*.]messenger.com (Shields off)" in capsys.readouterr().out


def test_reapply_same_patterns_is_noop(
    profile_root: Path, policy: dict, tmp_path: Path, capsys,
) -> None:
    cfg = _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com", "[*.]b.com"]\n')
    _apply(profile_root, cfg)
    capsys.readouterr()
    reordered = _config(tmp_path, '[shields]\ndisabled = ["[*.]b.com", "[*.]a.com"]\n')
    _apply(profile_root, reordered)

    assert policy["writes"] == ["shields"]
    assert "no changes" in capsys.readouterr().out


def test_removing_a_pattern_rewrites_and_empty_table_wipes(
    profile_root: Path, policy: dict, tmp_path: Path, capsys,
) -> None:
    _apply(profile_root, _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com", "[*.]b.com"]\n'))
    _apply(profile_root, _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com"]\n'))
    assert "- [*.]b.com (Shields back on)" in capsys.readouterr().out
    assert _read(policy["shields"])[shields.POLICY_KEY] == ["[*.]a.com"]

    _apply(profile_root, _config(tmp_path, "[shields]\n"))
    assert _read(policy["shields"])[shields.POLICY_KEY] == []


def test_missing_table_leaves_policy_alone(
    profile_root: Path, policy: dict, tmp_path: Path,
) -> None:
    _apply(profile_root, _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com"]\n'))
    _apply(profile_root, _config(tmp_path, '[pwa]\nurls = ["https://x.example/"]\n'))

    assert _read(policy["shields"])[shields.POLICY_KEY] == ["[*.]a.com"]


def test_pwa_and_shields_in_one_apply_both_survive(
    profile_root: Path, policy: dict, tmp_path: Path,
) -> None:
    """On macOS both land in one plist, written one after the other; the
    second write must merge onto the first, not replace it."""
    cfg = _config(tmp_path, (
        '[pwa]\nurls = ["https://x.example/"]\n'
        '[shields]\ndisabled = ["[*.]a.com"]\n'
    ))
    _apply(profile_root, cfg)

    assert _read(policy["shields"])[shields.POLICY_KEY] == ["[*.]a.com"]
    urls = [e["url"] for e in _read(policy["pwa"])[pwa.POLICY_KEY]]
    assert urls == ["https://x.example/"]


def test_skip_shields(
    profile_root: Path, policy: dict, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path, (
        '[pwa]\nurls = ["https://x.example/"]\n'
        '[shields]\ndisabled = ["[*.]a.com"]\n'
    ))
    _apply(profile_root, cfg, skip=["shields"])

    assert policy["writes"] == ["pwa"]
    assert not policy["shields"].exists() or shields.POLICY_KEY not in _read(policy["shields"])


def test_verification_failure_exits(
    profile_root: Path, policy: dict, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(shields, "_sudo_write_policy", lambda patterns: None)
    cfg = _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com"]\n')
    with pytest.raises(SystemExit) as e:
        _apply(profile_root, cfg)
    assert "shields verification failed" in str(e.value)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_block_only_when_policy_non_empty(
    profile_root: Path, policy: dict, tmp_path: Path,
) -> None:
    assert shields.build_dump_block() is None
    _apply(profile_root, _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com"]\n'))
    assert shields.build_dump_block() == [
        "[shields]", "disabled = [", '  "[*.]a.com",', "]",
    ]


@pytest.mark.parametrize("plat", ["linux", "win32"])
def test_pickup_note_never_promises_live_pickup_for_shields(monkeypatch, plat) -> None:
    """Measured on Brave 1.98 / Linux: the running browser listed the new
    BraveShieldsDisabledForUrls as "Restart required". The [pwa] timings
    must not be printed as if they covered it."""
    from dotbrave._base import orchestrator as orch

    monkeypatch.setattr(orch.sys, "platform", plat)
    assert "picks it up" in orch._policy_pickup_note(("pwa",))
    assert "restart" not in orch._policy_pickup_note(("pwa",))
    only = orch._policy_pickup_note(("shields",))
    assert "picks it up" not in only and "restart" in only
    both = orch._policy_pickup_note(("pwa", "shields"))
    assert "picks it up" in both and "[shields] takes full effect after a Brave restart" in both


def test_running_brave_apply_asks_for_the_shields_note(
    profile_root: Path, policy: dict, tmp_path: Path, monkeypatch, capsys,
) -> None:
    from dotbrave._base import orchestrator as orch

    asked: list = []
    real = orch._policy_pickup_note
    monkeypatch.setattr(
        orch, "_policy_pickup_note", lambda ns=("pwa",): asked.append(ns) or real(ns)
    )
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    _apply(profile_root, _config(tmp_path, '[shields]\ndisabled = ["[*.]a.com"]\n'))

    assert asked == [("shields",)]
    assert "[shields] applied without touching the running Brave" in capsys.readouterr().out
