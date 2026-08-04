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
SOURCE = Path(
    "/Library/Application Support/dotbrave/com.brave.Browser.managed.plist"
)


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
    assert str(source) in script
    assert str(BRAVE_PLIST) in script
    assert script.startswith("#!/bin/sh")
    # The idempotent guard and the cfprefsd refresh live in the shared data
    # file now, not the per-browser wrapper.
    heal_text = pwa.heal_script_source().read_text()
    # Idempotent guard: identical content must short-circuit before writing,
    # which is what breaks the WatchPaths -> write -> WatchPaths loop.
    assert "cmp -s" in heal_text
    # Refresh cfprefsd so the running browser/CFPreferences sees the value.
    assert "killall cfprefsd" in heal_text


def test_launchd_plist_watches_managed_prefs_and_runs_at_load() -> None:
    heal = Path("/Library/Application Support/dotbrave/com.brave.Browser.heal.sh")
    raw = pwa.build_launchd_plist("org.dotbrave.com.brave.Browser.pwa", heal,
                                  "/Library/Managed Preferences")
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == "org.dotbrave.com.brave.Browser.pwa"
    assert parsed["ProgramArguments"] == ["/bin/sh", str(heal)]
    assert parsed["WatchPaths"] == ["/Library/Managed Preferences"]
    assert parsed["RunAtLoad"] is True


def test_launchd_plist_heals_faster_than_a_login_item_browser_starts() -> None:
    """macOS prunes orphan plists from /Library/Managed Preferences during
    boot. If the browser auto-starts before the daemon restores the policy it
    reads an empty ``WebAppInstallForceList`` and uninstalls every managed PWA
    -- and it will not reload the policy while running. So the daemon must
    react to the WatchPaths event within about a second, not on launchd's
    10-second default throttle, and must still recover if the event is missed
    entirely."""
    raw = pwa.build_launchd_plist(
        "org.dotbrave.com.brave.Browser.pwa",
        Path("/Library/Application Support/dotbrave/com.brave.Browser.heal.sh"),
        "/Library/Managed Preferences",
    )
    parsed = plistlib.loads(raw)
    # Must be set explicitly: omitting the key leaves launchd's 10s default,
    # which is the delay that loses the race.
    assert parsed["ThrottleInterval"] == 1
    # Safety net for a dropped WatchPaths notification.
    assert parsed["StartInterval"] == 60


def test_heal_log_sits_beside_the_other_support_files() -> None:
    assert pwa.macos_heal_log(BRAVE_PLIST) == Path(
        "/Library/Application Support/dotbrave/com.brave.Browser.heal.log"
    )


def test_heal_script_lifts_the_immutable_flag_around_its_write() -> None:
    """The policy file is pinned `schg` so macOS's boot-time reconcile cannot
    unlink it. That same flag blocks the daemon's own `cp`, so the script must
    lift it, write, and re-pin -- in that order."""
    # The lift-write-pin dance lives in the shared data file now.
    script = pwa.heal_script_source().read_text()
    lift = script.index("chflags noschg")
    write = script.index('/bin/cp "$SRC" "$DEST"')
    pin = script.index("chflags schg")
    assert lift < write < pin


def test_heal_script_records_every_heal_for_post_boot_forensics() -> None:
    """A heal at boot means the browser may already have read an empty policy
    and uninstalled its PWAs. The log is the only evidence that survives to be
    read after the fact -- a notification at that point in boot has nobody to
    show itself to."""
    built = pwa.build_heal_script(SOURCE, BRAVE_PLIST)
    assert str(pwa.macos_heal_log(BRAVE_PLIST)) in built
    # Only actual heals are logged: the cmp short-circuit comes first. That
    # ordering is data-file logic now, over the generic $LOG variable.
    heal_text = pwa.heal_script_source().read_text()
    assert heal_text.index("cmp -s") < heal_text.index('>> "$LOG"')


def test_heal_script_warns_the_console_user_when_one_exists() -> None:
    # Notification logic lives in the shared data file now.
    script = pwa.heal_script_source().read_text()
    # A root daemon cannot post to the user's session directly.
    assert "launchctl asuser" in script
    assert "osascript" in script
    # Best-effort only -- never let a missing GUI session fail the heal.
    assert script.rstrip().endswith("exit 0")


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
    assert {daemon, str(source), str(heal),
            str(pwa.macos_heal_log(BRAVE_PLIST))} <= removed


def test_remove_daemon_unpins_the_policy_file() -> None:
    """Teardown must leave nothing immutable behind, or a later uninstall --
    or an OS upgrade -- trips over a file nothing can delete."""
    rec = _Recorder()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pwa.subprocess, "run", rec)
        pwa.remove_self_healing_daemon(BRAVE_PLIST)
    assert ["sudo", "chflags", "noschg", str(BRAVE_PLIST)] in rec.calls


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


def test_darwin_write_unpins_before_writing_and_repins_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`sudo tee` cannot overwrite an schg file either, so the privileged
    writer owns the same lift-write-pin dance as the heal script."""
    _force_darwin(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)
    monkeypatch.setattr(pwa, "install_self_healing_daemon", lambda pf, c: None)

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [{"url": "https://a/"}])

    flat = [" ".join(c) for c in rec.calls]
    lift = flat.index(f"sudo chflags noschg {policy_file}")
    write = flat.index(f"sudo tee {policy_file}")
    pin = flat.index(f"sudo chflags schg {policy_file}")
    assert lift < write < pin


def test_darwin_write_seeds_the_daemon_source_before_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The daemon reverts anything that disagrees with its source plist, and
    at ThrottleInterval 1 it reacts inside the window of our own apply. Seed
    the source first so a heal firing mid-write compares against the content
    we are writing rather than restoring the policy we are replacing."""
    _force_darwin(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)
    monkeypatch.setattr(pwa, "install_self_healing_daemon", lambda pf, c: None)

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [{"url": "https://a/"}])

    source, _ = pwa.macos_support_paths(policy_file)
    flat = [" ".join(c) for c in rec.calls]
    assert flat.index(f"sudo tee {source}") < flat.index(f"sudo tee {policy_file}")


def test_darwin_empty_table_stops_the_daemon_before_emptying_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Removal has no new content to seed the daemon with, so the only way to
    stop it restoring the entries being removed is to boot it out first."""
    _force_darwin(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)
    monkeypatch.setattr(pwa, "remove_self_healing_daemon", lambda pf: None)

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [])

    daemon = str(pwa.macos_daemon_path(policy_file))
    flat = [" ".join(c) for c in rec.calls]
    stop = flat.index(f"sudo launchctl bootout system {daemon}")
    assert stop < flat.index(f"sudo tee {policy_file}")


def test_empty_table_leaves_the_policy_file_unpinned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An empty [pwa] table tears the daemon down; pinning the leftover file
    would strand an immutable artifact with nothing left to maintain it."""
    _force_darwin(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(pwa.subprocess, "run", rec)
    monkeypatch.setattr(pwa, "remove_self_healing_daemon", lambda pf: None)

    policy_file = tmp_path / "com.brave.Browser.plist"
    pwa.sudo_write_policy(policy_file, "", [])

    flat = [" ".join(c) for c in rec.calls]
    assert f"sudo chflags noschg {policy_file}" in flat
    assert f"sudo chflags schg {policy_file}" not in flat


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


def test_heal_script_ships_as_a_data_file() -> None:
    """Nix module dùng chung file này, nên nó phải tồn tại trên đĩa."""
    src = pwa.heal_script_source()
    assert src.is_file()
    text = src.read_text()
    assert 'cmp -s "$SRC" "$DEST"' in text
    assert "chflags noschg" in text
    assert "chflags schg" in text
    assert "killall cfprefsd" in text


def test_heal_script_reads_paths_from_environment() -> None:
    """Không hardcode đường dẫn -- Python và Nix truyền qua env."""
    text = pwa.heal_script_source().read_text()
    for hardcoded in ("/Library/Managed Preferences", "com.brave.Browser"):
        assert hardcoded not in text


def test_built_script_sets_env_and_execs_the_data_file() -> None:
    built = pwa.build_heal_script(SOURCE, BRAVE_PLIST)
    assert f'SRC="{SOURCE}"' in built
    assert f'DEST="{BRAVE_PLIST}"' in built
    assert str(pwa.heal_script_source()) in built


def test_heal_script_creates_its_log_directory() -> None:
    """LOG có thể nằm trong thư mục Nix chưa tạo."""
    assert 'mkdir -p "$(dirname "$LOG")"' in pwa.heal_script_source().read_text()
