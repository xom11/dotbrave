"""End-to-end tests for `brave apply` exercising the [pwa] table.

The pwa module is the first dotbrave surface to (a) own state outside
the user's profile (Brave's managed-policy file / Windows registry) and
(b) require elevated privileges to apply. These tests bypass both:
`POLICY_FILE` is redirected into the per-test `tmp_path` (Linux/macOS),
and `_sudo_write_policy` / the orchestrator's privilege preflight are
replaced with no-escalation stubs. On Windows the fake writes plain
JSON to a temp file instead of the registry.

That isolation lets the suite run unattended in CI without ever touching
real /etc/, /Library/, the registry, or prompting for credentials.

The fake `_sudo_write_policy` calls into the real `_build_policy_payload`
(Linux/macOS) so the platform-specific serialization is exercised live —
only the privileged install step is faked.
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
from dotbrave import pwa

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin" or sys.platform == "win32"),
    reason="pwa apply path is implemented for Linux, macOS and Windows",
)


def _read_policy_file(path: Path) -> dict:
    """Parse the policy file using the right format for the current platform.

    On Windows the fake fixture writes plain JSON (same as Linux) because
    the real Windows path uses the registry, not a file.
    """
    if sys.platform == "darwin":
        with path.open("rb") as f:
            return plistlib.load(f)
    return json.loads(path.read_text())


@pytest.fixture
def fake_pwa_profile_root(tmp_path: Path) -> Path:
    """Minimal profile that's just enough for the orchestrator to load.

    pwa doesn't touch Preferences, but the orchestrator still loads it,
    so we need a syntactically valid file. Anything goes for the body.
    """
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"some": "thing"}))
    return tmp_path


@pytest.fixture
def fake_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the managed-policy storage into tmp + neutralize elevation.

    On Linux/macOS:
    1. ``pwa.POLICY_FILE`` -> tmp path so ``_read_current_policy()`` finds
       per-test state and read-back verification works.
    2. ``pwa._sudo_write_policy`` -> direct write (no sudo), but it still
       calls ``_build_policy_payload`` so the live serializer runs.
    3. ``brave_pkg.subprocess.run`` -> swallow the orchestrator's
       ``sudo -v`` preflight; everything else passes through.

    On Windows the real path is the registry. We redirect both read and
    write to a plain JSON file in tmp and stub the admin check.
    """
    if sys.platform == "darwin":
        fake_path = tmp_path / "policy" / "com.brave.Browser.plist"
    else:
        # Both Windows and Linux use a JSON file for the fake
        fake_path = tmp_path / "policy" / "dotbrave-pwa.json"

    if sys.platform == "win32":
        # On Windows the real read path goes through _read_windows_registry_payload.
        # Redirect it to read from our fake JSON file instead.
        def fake_read_payload() -> dict:
            if not fake_path.exists():
                return {}
            try:
                return json.loads(fake_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}

        monkeypatch.setattr(pwa, "_read_existing_payload", fake_read_payload)

        def fake_sudo_write_policy(entries: list[dict]) -> None:
            fake_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {pwa.POLICY_KEY: entries}
            fake_path.write_text(json.dumps(payload, indent=2))

        monkeypatch.setattr(pwa, "_sudo_write_policy", fake_sudo_write_policy)

        # Stub the admin check so the orchestrator's preflight passes
        import ctypes
        monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    else:
        monkeypatch.setattr(pwa, "POLICY_FILE", fake_path)

        def fake_sudo_write_policy(entries: list[dict]) -> None:
            fake_path.parent.mkdir(parents=True, exist_ok=True)
            fake_path.write_bytes(pwa._build_policy_payload(entries))

        monkeypatch.setattr(pwa, "_sudo_write_policy", fake_sudo_write_policy)

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if list(cmd[:3]) == ["sudo", "-n", "true"]:
                return subprocess.CompletedProcess(cmd, 0)
            if list(cmd[:2]) == ["sudo", "-v"]:
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        from dotbrave._base import orchestrator as orch
        monkeypatch.setattr(orch.subprocess, "run", fake_run)
    return fake_path


def _apply(profile_root: Path, config: Path) -> None:
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=config,
        dry_run=False,
    )
    brave_pkg.cmd_apply(args)


# ---------------------------------------------------------------------------
# _validate_table — pure logic, no fixtures needed
# ---------------------------------------------------------------------------


def test_validate_accepts_simple_url_list() -> None:
    out = pwa._validate_table(
        {"urls": ["https://squoosh.app/", "https://app.element.io/"]}
    )
    assert out == ["https://squoosh.app/", "https://app.element.io/"]


def test_validate_dedupes_preserves_order() -> None:
    out = pwa._validate_table(
        {"urls": ["https://a/", "https://b/", "https://a/"]}
    )
    assert out == ["https://a/", "https://b/"]


def test_validate_empty_table_is_wipe() -> None:
    """Empty `[pwa]` (no `urls`) and `urls = []` are both the explicit
    "wipe all my managed PWAs" gesture — neither is an error."""
    assert pwa._validate_table({}) == []
    assert pwa._validate_table({"urls": []}) == []


def test_validate_rejects_non_table() -> None:
    with pytest.raises(SystemExit, match=r"\[pwa\] must be a table"):
        pwa._validate_table(["https://nope/"])


def test_validate_rejects_non_list_urls() -> None:
    with pytest.raises(SystemExit, match=r"urls must be an array"):
        pwa._validate_table({"urls": "https://nope/"})


def test_validate_rejects_non_string_url_entries() -> None:
    with pytest.raises(SystemExit, match=r"url entries must be strings"):
        pwa._validate_table({"urls": [123]})


def test_validate_rejects_non_https_urls() -> None:
    with pytest.raises(SystemExit, match=r"must start with https://"):
        pwa._validate_table({"urls": ["javascript:alert(1)"]})
    with pytest.raises(SystemExit, match=r"must start with https://"):
        pwa._validate_table({"urls": ["squoosh.app"]})  # missing scheme
    with pytest.raises(SystemExit, match=r"must start with https://"):
        pwa._validate_table({"urls": ["http://example.com/"]})  # plain http


def test_validate_rejects_unknown_keys() -> None:
    """Reject keys we don't yet support so that future schema additions
    can rely on never having silently absorbed user intent. v1 only
    supports `urls = [...]`."""
    with pytest.raises(SystemExit, match=r"unsupported keys"):
        pwa._validate_table({"urls": [], "name": "wat"})


# ---------------------------------------------------------------------------
# End-to-end via cmd_apply
# ---------------------------------------------------------------------------


def test_first_apply_writes_policy_file(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first apply should create the policy file with our defaults."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[pwa]\n'
        'urls = ["https://squoosh.app/", "https://app.element.io/"]\n'
    )
    _apply(fake_pwa_profile_root, cfg)

    assert fake_policy.exists()
    data = _read_policy_file(fake_policy)
    entries = data[pwa.POLICY_KEY]
    urls = sorted(e["url"] for e in entries)
    assert urls == ["https://app.element.io/", "https://squoosh.app/"]
    # Defaults applied to every entry
    for e in entries:
        assert e["default_launch_container"] == "window"
        assert e["create_desktop_shortcut"] is True


def test_reapply_same_urls_is_noop(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency: re-applying the same config should report no
    changes and not re-write the policy file (mtime unchanged)."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)
    mtime_before = fake_policy.stat().st_mtime_ns

    _apply(fake_pwa_profile_root, cfg)
    assert fake_policy.stat().st_mtime_ns == mtime_before, \
        "policy file rewritten when no diff was expected"


def test_remove_url_uninstalls(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a URL from config should drop it from the policy file
    on next apply — Brave then uninstalls the app at next launch."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[pwa]\n'
        'urls = ["https://squoosh.app/", "https://app.element.io/"]\n'
    )
    _apply(fake_pwa_profile_root, cfg)

    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)

    data = _read_policy_file(fake_policy)
    urls = [e["url"] for e in data[pwa.POLICY_KEY]]
    assert urls == ["https://squoosh.app/"]


def test_empty_pwa_table_wipes(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[pwa]` with no body → policy file becomes an empty list (NOT a
    deleted file). Brave reads the empty list and uninstalls every
    previously force-installed app. Keeping the file around preserves
    the dotbrave-managed marker for anyone inspecting /etc/."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)

    cfg.write_text('[pwa]\n')  # header only, no urls
    _apply(fake_pwa_profile_root, cfg)

    assert fake_policy.exists()
    data = _read_policy_file(fake_policy)
    assert data[pwa.POLICY_KEY] == []


def test_missing_pwa_table_leaves_policy_alone(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOML without `[pwa]` must NOT touch the policy file (same
    skip-on-missing semantics as the other modules)."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)
    before_bytes = fake_policy.read_bytes()

    # Now apply something else entirely — pwa table absent.
    cfg.write_text(
        '[settings]\n'
        '"some.unrelated.key" = true\n'
    )
    _apply(fake_pwa_profile_root, cfg)

    assert fake_policy.read_bytes() == before_bytes


def test_pwa_does_not_create_state_sidecar(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike shortcuts/settings, pwa owns its own external state file
    (`POLICY_FILE`) so it must NOT also produce a sidecar next to
    Preferences. Auditing /etc/brave/policies/managed/ should be the
    only place to check what dotbrave has installed."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)

    profile_dir = fake_pwa_profile_root / "Default"
    sidecars = list(profile_dir.glob("Preferences.dotbrave.pwa*"))
    assert sidecars == [], f"unexpected sidecar(s): {sidecars}"


def test_dump_emits_managed_urls(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`pwa dump` should emit a TOML doc that round-trips through the
    parser back to the same URL set."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[pwa]\n'
        'urls = ["https://squoosh.app/", "https://app.element.io/"]\n'
    )
    _apply(fake_pwa_profile_root, cfg)
    capsys.readouterr()  # drain apply's progress output

    args = argparse.Namespace(
        profile_root=fake_pwa_profile_root,
        profile="Default",
        output=None,
    )
    pwa.cmd_dump(args)
    out = capsys.readouterr().out

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore

    parsed = tomllib.loads(out)
    assert sorted(parsed["pwa"]["urls"]) == [
        "https://app.element.io/",
        "https://squoosh.app/",
    ]


def test_dump_first_run_shows_empty_list_with_note(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """First-run dump (no policy file yet) is not an error — emit
    `urls = []` with a note so users can copy-paste it as a starting
    config without seeing a stack trace."""
    args = argparse.Namespace(
        profile_root=fake_pwa_profile_root,
        profile="Default",
        output=None,
    )
    pwa.cmd_dump(args)
    out = capsys.readouterr().out
    assert "[pwa]" in out
    assert "urls = []" in out
    assert "no managed PWAs" in out


# ---------------------------------------------------------------------------
# macOS-specific: read-modify-write must preserve unrelated MDM keys
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="MDM-key preservation is a macOS-only concern (Linux file is namespaced)",
)
def test_macos_preserves_unrelated_mdm_keys(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/Library/Managed Preferences/com.brave.Browser.plist` is keyed
    by Brave's bundle ID, so an MDM may already be writing other policy
    keys to the same file. dotbrave must round-trip without clobbering
    them — only its own `WebAppInstallForceList` key gets touched.
    """
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    # Pre-seed the plist with an unrelated MDM-managed key. This is the
    # state we'd find on a managed Mac whose admin already pushed e.g.
    # `HomepageLocation` and `URLBlocklist` via a configuration profile.
    fake_policy.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "HomepageLocation": "https://intranet.example.com/",
        "URLBlocklist": ["example.com"],
    }
    with fake_policy.open("wb") as f:
        plistlib.dump(seed, f, fmt=plistlib.FMT_BINARY)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)

    data = _read_policy_file(fake_policy)
    # Our key was written.
    assert [e["url"] for e in data[pwa.POLICY_KEY]] == ["https://squoosh.app/"]
    # Unrelated keys survived intact.
    assert data["HomepageLocation"] == "https://intranet.example.com/"
    assert data["URLBlocklist"] == ["example.com"]


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="binary plist format is only what the macOS path produces",
)
def test_macos_uses_binary_plist_format(
    fake_pwa_profile_root: Path,
    fake_policy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-check the on-disk format is binary plist, not XML or JSON.
    Binary matches what `defaults` writes and is what tooling like
    `plutil -p` expects without explicit format flags."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://squoosh.app/"]\n')
    _apply(fake_pwa_profile_root, cfg)

    # Binary plist files start with the magic bytes "bplist00".
    assert fake_policy.read_bytes().startswith(b"bplist00")
