"""Unit tests for the macOS self-healing PWA LaunchDaemon.

These exercise the darwin durability machinery in _base/pwa.py. The pure
builders run on any platform; the privileged install/remove functions are
tested by recording the subprocess commands they would run (no real sudo,
no real launchctl).
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from dotbrave._base import pwa


BRAVE_PLIST = Path("/Library/Managed Preferences/com.brave.Browser.plist")


def test_bundle_id_is_basename_without_suffix() -> None:
    assert pwa.macos_bundle_id(BRAVE_PLIST) == "com.brave.Browser"


def test_support_paths_namespaced_by_bundle() -> None:
    source, heal = pwa.macos_support_paths(BRAVE_PLIST)
    assert source == Path(
        "/Library/Application Support/dotbrave/com.brave.Browser.managed.plist"
    )
    assert heal == Path(
        "/Library/Application Support/dotbrave/com.brave.Browser.heal.sh"
    )


def test_daemon_label_and_path() -> None:
    assert pwa.macos_daemon_label(BRAVE_PLIST) == "org.dotbrave.com.brave.Browser.pwa"
    assert pwa.macos_daemon_path(BRAVE_PLIST) == Path(
        "/Library/LaunchDaemons/org.dotbrave.com.brave.Browser.pwa.plist"
    )


def test_heal_script_is_idempotent_and_refreshes_cfprefsd() -> None:
    source = Path("/Library/Application Support/dotbrave/com.brave.Browser.managed.plist")
    script = pwa.build_heal_script(source, BRAVE_PLIST)
    # Idempotent guard: identical content must short-circuit before writing,
    # which is what breaks the WatchPaths -> write -> WatchPaths loop.
    assert "cmp -s" in script
    assert str(source) in script
    assert str(BRAVE_PLIST) in script
    # Refresh cfprefsd so the running browser/CFPreferences sees the value.
    assert "killall cfprefsd" in script
    assert script.startswith("#!/bin/sh")


def test_launchd_plist_watches_managed_prefs_and_runs_at_load() -> None:
    heal = Path("/Library/Application Support/dotbrave/com.brave.Browser.heal.sh")
    raw = pwa.build_launchd_plist("org.dotbrave.com.brave.Browser.pwa", heal,
                                  "/Library/Managed Preferences")
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == "org.dotbrave.com.brave.Browser.pwa"
    assert parsed["ProgramArguments"] == ["/bin/sh", str(heal)]
    assert parsed["WatchPaths"] == ["/Library/Managed Preferences"]
    assert parsed["RunAtLoad"] is True
    assert parsed["ThrottleInterval"] == 10


class _Recorder:
    """Records subprocess.run invocations instead of executing them."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        import subprocess
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)


def test_install_daemon_writes_root_owned_files_and_bootstraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)

    pwa.install_self_healing_daemon(BRAVE_PLIST, b"<plist/>")

    flat = [" ".join(c) for c in rec.calls]
    # Source, script, and daemon are all chowned root:wheel (no escalation).
    assert sum("chown root:wheel" in f for f in flat) == 3
    # Daemon plist is mode 0644, heal script 0755, source plist 0644.
    assert any("chmod 0755" in f and "heal.sh" in f for f in flat)
    assert any("chmod 0644" in f and ".pwa.plist" in f for f in flat)
    assert any("chmod 0644" in f and "managed.plist" in f for f in flat)
    # Reload: bootout (ignored if absent) then bootstrap into the system domain.
    daemon = str(pwa.macos_daemon_path(BRAVE_PLIST))
    assert ["sudo", "launchctl", "bootout", "system", daemon] in rec.calls
    assert ["sudo", "launchctl", "bootstrap", "system", daemon] in rec.calls


def test_remove_daemon_boots_out_and_deletes_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)

    pwa.remove_self_healing_daemon(BRAVE_PLIST)

    daemon = str(pwa.macos_daemon_path(BRAVE_PLIST))
    source, heal = pwa.macos_support_paths(BRAVE_PLIST)
    assert ["sudo", "launchctl", "bootout", "system", daemon] in rec.calls
    removed = {c[-1] for c in rec.calls if c[:3] == ["sudo", "rm", "-f"]}
    assert {daemon, str(source), str(heal)} <= removed


def _force_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwa.sys, "platform", "darwin")


def test_nonempty_entries_install_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(pwa.subprocess, "run", _Recorder())
    installed: list = []
    removed: list = []
    monkeypatch.setattr(pwa, "install_self_healing_daemon",
                        lambda pf, content: installed.append((pf, content)))
    monkeypatch.setattr(pwa, "remove_self_healing_daemon",
                        lambda pf: removed.append(pf))

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [{"url": "https://a/"}])

    assert len(installed) == 1 and installed[0][0] == policy_file
    # The daemon must be seeded with exactly the bytes written to the
    # managed plist, so the heal script's cmp -s compares like-for-like.
    assert installed[0][1] == pwa.build_policy_payload(
        policy_file, "", [{"url": "https://a/"}]
    )
    assert removed == []


def test_empty_entries_remove_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(pwa.subprocess, "run", _Recorder())
    installed: list = []
    removed: list = []
    monkeypatch.setattr(pwa, "install_self_healing_daemon",
                        lambda pf, content: installed.append(pf))
    monkeypatch.setattr(pwa, "remove_self_healing_daemon",
                        lambda pf: removed.append(pf))

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [])

    assert installed == []
    assert removed == [policy_file]


def test_non_darwin_skips_daemon_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(pwa.sys, "platform", "linux")
    monkeypatch.setattr(pwa.subprocess, "run", _Recorder())
    installed: list = []
    removed: list = []
    monkeypatch.setattr(pwa, "install_self_healing_daemon",
                        lambda pf, content: installed.append((pf, content)))
    monkeypatch.setattr(pwa, "remove_self_healing_daemon",
                        lambda pf: removed.append(pf))

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [{"url": "https://a/"}])

    assert installed == []
    assert removed == []
