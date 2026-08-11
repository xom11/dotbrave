"""Process detection/close must be scoped to the target --user-data-dir.

Regression tests for the Linux bug where a stable-channel apply against
one profile root closed *every* running Brave (`pgrep`/`pkill -x brave`
are global).  On Linux an explicitly-launched Brave keeps
``--user-data-dir=<root>`` on its main process and all children, while a
default-launched Brave (opened from the app menu) carries the flag
nowhere -- so a pid's own cmdline is enough to tell which instance/root
it belongs to.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


def _linux_process_module(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    from dotbrave._base import process as bp
    importlib.reload(bp)
    return bp


def _make_proc(bp, *, linux_pid_filter=None):
    return bp.BrowserProcess(
        display_name="Brave",
        proc_name_linux="brave",
        proc_name_macos="Brave Browser",
        proc_name_windows="brave.exe",
        macos_app_name="Brave Browser",
        linux_wrappers=["brave-browser"],
        windows_exe_relpath=(
            "BraveSoftware", "Brave-Browser", "Application", "brave.exe",
        ),
        linux_pid_filter=linux_pid_filter,
    )


DEFAULT_ROOT = "/home/u/.config/BraveSoftware/Brave-Browser"
OTHER_ROOT = "/tmp/udd"


def _wire_pgrep(monkeypatch, bp, cmdlines):
    pids = "".join(f"{p}\n" for p in cmdlines)
    monkeypatch.setattr(
        bp.subprocess, "check_output", lambda *a, **kw: pids.encode()
    )
    monkeypatch.setattr(bp, "_read_cmdline", lambda pid: cmdlines.get(pid))


def test_scope_excludes_other_profile_root(monkeypatch) -> None:
    """The exact bug: a default-launched Brave (flagless) must NOT be
    seen as running when we target a *different* explicit root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        # default-launched Brave the user is actively using
        "100": ["/opt/brave.com/brave/brave"],
        "101": ["/opt/brave.com/brave/brave", "--type=renderer"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == []
    assert proc.running() is False


def test_scope_matches_explicit_target(monkeypatch) -> None:
    """Only the instance whose cmdline carries --user-data-dir=<target>
    is selected; a co-running default instance is left out."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # default, untouched
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}",
                "--profile-directory=Default"],  # target main
        "201": ["/opt/brave.com/brave/brave", "--type=zygote",
                f"--user-data-dir={OTHER_ROOT}"],  # target child
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == ["200", "201"]
    assert proc.running() is True


def test_scope_default_target_includes_flagless_and_matching(monkeypatch) -> None:
    """Targeting the default root selects both flagless (menu-launched)
    Brave and any instance explicitly pointed at the default root, but
    excludes a second explicit root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # flagless default
        "110": ["/opt/brave.com/brave/brave", f"--user-data-dir={DEFAULT_ROOT}"],
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp)
    proc.scope_to_profile(DEFAULT_ROOT, default_user_data_dir=DEFAULT_ROOT)

    assert proc.pids() == ["100", "110"]


def test_close_and_wait_scoped_kill_not_global_pkill(monkeypatch) -> None:
    """With a profile scope active on stable (no channel filter),
    close_and_wait must SIGTERM only the scoped pids -- never
    `pkill -TERM -x brave`, which would hit the user's other Brave."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],  # default -- must survive
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.close_and_wait(timeout=0.2)

    assert calls == [["kill", "-TERM", "200"]]


def test_kill_and_wait_scoped_kill_not_global_pkill(monkeypatch) -> None:
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],
        "200": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait(timeout=0.2)

    assert calls == [["kill", "-KILL", "200"]]


def test_unscoped_behavior_unchanged(monkeypatch) -> None:
    """Without a scope set, stable keeps the permissive global pgrep/pkill
    behavior (Snap/Flatpak installs rely on it)."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        "100": ["/opt/brave.com/brave/brave"],
        "200": ["/snap/brave/x/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        bp.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd))
        or bp.subprocess.CompletedProcess(cmd, 0),
    )

    proc = _make_proc(bp)  # no scope_to_profile
    assert proc.pids() == ["100", "200"]
    monkeypatch.setattr(proc, "running", lambda: False)
    proc.kill_and_wait(timeout=0.2)
    assert calls == [["pkill", "-KILL", "-x", "brave"]]


def test_scope_combines_with_channel_filter(monkeypatch) -> None:
    """Beta channel + profile scope: a pid must match BOTH the channel
    path filter and the target root."""
    bp = _linux_process_module(monkeypatch)
    cmdlines = {
        # beta at target root -- keep
        "100": ["/opt/brave.com/brave-beta/brave", f"--user-data-dir={OTHER_ROOT}"],
        # beta at a different root -- drop
        "200": ["/opt/brave.com/brave-beta/brave", "--user-data-dir=/tmp/z"],
        # stable at target root -- drop (wrong channel)
        "300": ["/opt/brave.com/brave/brave", f"--user-data-dir={OTHER_ROOT}"],
    }
    _wire_pgrep(monkeypatch, bp, cmdlines)

    proc = _make_proc(bp, linux_pid_filter="/opt/brave.com/brave-beta/")
    proc.scope_to_profile(OTHER_ROOT, default_user_data_dir=DEFAULT_ROOT)
    assert proc.pids() == ["100"]


# --------------------------------------------------------------------------
# Live apply's work tab must belong to the profile the run is bound to.
#
# `CdpClient.create_page` issues `PUT /json/new`, which carries no profile
# hint: upstream builds the new target's NavigateParams with
# `ProfileManager::GetLastUsedProfile()`
# (chrome/browser/devtools/chrome_devtools_manager_delegate.cc), so the work
# tab lands in the browser's *last-used* profile while the diff, the backup,
# the sidecars and verify_fn are all bound to `args.profile`.  Endpoint
# discovery does not save us either: only the `.dotbrave.live.json` sidecar
# is profile-aware, and both fallbacks (`DevToolsActivePort` and the running
# command line) are profile-blind.  So confirm the tab's own profile before
# writing through it, and refuse when it cannot be confirmed -- a visible
# close/relaunch beats a silent cross-profile write.
# --------------------------------------------------------------------------


def _live_run(
    tmp_path: Path,
    monkeypatch,
    *,
    profile: str,
    seen: str | None,
    refuse_create: bool = False,
):
    """Wire `live.apply_live` against `<tmp_path>/<profile>/Preferences`.

    `seen` is what the fake endpoint's `chrome://version` reports as the
    work tab's own profile path.
    """
    from tests.test_brave_live import FakeCdpClient
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / profile / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs = {"brave": {"location_bar_is_wide": False}}
    prefs_path.write_text(json.dumps(prefs))

    fake = FakeCdpClient(9333, evaluation_results=[[]], profile_path=seen)
    fake.refuse_create = refuse_create
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=lambda t: t["brave"].__setitem__("location_bar_is_wide", True),
        verify_fn=lambda _p: None,
        state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
        state_payload={"managed_keys": ["brave.location_bar_is_wide"]},
    )
    return live, fake, prefs_path, prefs, plan


def test_live_apply_proceeds_when_the_work_tab_is_the_target_profile(
    tmp_path: Path, monkeypatch
) -> None:
    live, fake, prefs_path, prefs, plan = _live_run(
        tmp_path, monkeypatch,
        profile="Profile 2", seen=str(tmp_path / "Profile 2"),
    )

    live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")

    assert any("chrome://version" in url for url in fake.navigations)
    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
    assert plan.state_path.exists()


def test_live_apply_refuses_a_work_tab_from_the_wrong_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """The bug this exists to remove: the tab is in `Default`, the run is
    bound to `Profile 2`.  Nothing may be written through it."""
    from dotbrave._base import live_apply as shared_live

    live, fake, prefs_path, prefs, plan = _live_run(
        tmp_path, monkeypatch,
        profile="Profile 2", seen=str(tmp_path / "Default"),
    )

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")

    # Named, so the fallback's report says which profile went unconfirmed.
    assert any("Profile 2" in key for key in excinfo.value.keys)
    # Nothing was pushed, in either profile.
    assert not any("setPref(" in e for e in fake.evaluations)
    assert not plan.state_path.exists()
    # The check precedes the backup, so an unconfirmed profile leaves none
    # behind and the offline path keeps its own (backup_taken stays False).
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []
    assert excinfo.value.backup_taken is False
    # The tab we opened is still cleaned up.
    assert fake.closed == fake.created != []


@pytest.mark.parametrize("seen", ["", "   ", None])
def test_live_apply_refuses_when_the_work_tab_profile_is_unreadable(
    tmp_path: Path, monkeypatch, seen
) -> None:
    """Element missing, empty text, or a non-string result: all mean the
    profile is unconfirmed, which fails closed exactly like a mismatch."""
    from dotbrave._base import live_apply as shared_live

    live, fake, prefs_path, prefs, plan = _live_run(
        tmp_path, monkeypatch, profile="Profile 2", seen=seen,
    )

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")

    assert any("Profile 2" in key for key in excinfo.value.keys)
    assert not any("setPref(" in e for e in fake.evaluations)
    assert not plan.state_path.exists()
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []
    assert excinfo.value.backup_taken is False


def test_live_apply_confirms_the_profile_of_a_reused_page_too(
    tmp_path: Path, monkeypatch
) -> None:
    """`_worker_target` falls back to an existing page when the endpoint
    refuses /json/new.  That tab is even more likely to belong to another
    profile -- and we never close it -- so it needs the same check."""
    from dotbrave._base import live_apply as shared_live

    live, fake, prefs_path, prefs, plan = _live_run(
        tmp_path, monkeypatch,
        profile="Profile 2", seen=str(tmp_path / "Default"),
        refuse_create=True,
    )

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")

    assert any("Profile 2" in key for key in excinfo.value.keys)
    assert fake.created == []          # the fallback reused a user tab...
    assert fake.closed == []           # ...and we must not close it
    assert not any("setPref(" in e for e in fake.evaluations)
    assert list(prefs_path.parent.glob("Preferences.bak.*")) == []


def test_live_apply_tolerates_a_trailing_separator_and_a_symlinked_root(
    tmp_path: Path, monkeypatch
) -> None:
    """chrome://version prints `profile_path.LossyDisplayName()` -- the
    path Brave was *given*, not an absolutised one -- so compare resolved
    paths, not strings."""
    root = tmp_path / "real"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)

    live, fake, prefs_path, prefs, plan = _live_run(
        root, monkeypatch,
        profile="Profile 2", seen=str(link / "Profile 2") + os.sep,
    )

    live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")

    assert any(
        "brave.location_bar_is_wide" in e and "setPref" in e
        for e in fake.evaluations
    )
