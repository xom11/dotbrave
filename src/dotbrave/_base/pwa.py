"""Shared PWA (Progressive Web App) logic for Chromium-based browsers.

All Chromium browsers honor the ``WebAppInstallForceList`` enterprise
policy.  This module provides the shared validation, diffing, and
I/O logic.  Browser-specific modules configure paths and provide thin
wrappers (so tests can monkeypatch module-level ``POLICY_FILE`` and
``_sudo_write_policy`` per browser).
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import winreg

from dotbrave._base.utils import Plan, find_preferences

NAMESPACE = "pwa"
POLICY_KEY = "WebAppInstallForceList"

_DEFAULT_ENTRY = {
    "default_launch_container": "window",
    "create_desktop_shortcut": True,
}

_MACOS_SUPPORT_DIR = Path("/Library/Application Support/dotbrave")
_MACOS_LAUNCHD_DIR = Path("/Library/LaunchDaemons")
# Intentionally a str (not a Path): used directly as a launchd plist value.
# All per-browser daemons share this single watch directory; the heal
# script's ``cmp -s`` idempotent guard makes the resulting cross-triggers
# harmless (a trigger for browser A runs browser A's script, which exits
# immediately if its source and managed plists are already identical).
_MACOS_WATCH_DIR = "/Library/Managed Preferences"


def macos_bundle_id(policy_file: Path) -> str:
    """Bundle id derived from the managed plist name, e.g.
    ``/Library/Managed Preferences/com.brave.Browser.plist`` ->
    ``com.brave.Browser``."""
    return policy_file.stem


def macos_support_paths(policy_file: Path) -> tuple[Path, Path]:
    """(source-of-truth plist, heal script) paths for this browser."""
    bundle = macos_bundle_id(policy_file)
    source = _MACOS_SUPPORT_DIR / f"{bundle}.managed.plist"
    heal = _MACOS_SUPPORT_DIR / f"{bundle}.heal.sh"
    return source, heal


def macos_heal_log(policy_file: Path) -> Path:
    """Where the daemon records that it had to heal.

    Boot-time heals are the ones that matter -- they mean the browser may
    already have read an empty policy -- and at that point in boot there is no
    user session to notify, so this file is the only evidence left to read
    afterwards."""
    return _MACOS_SUPPORT_DIR / f"{macos_bundle_id(policy_file)}.heal.log"


def macos_daemon_label(policy_file: Path) -> str:
    """LaunchDaemon label for this browser, e.g. ``org.dotbrave.com.brave.Browser.pwa``."""
    return f"org.dotbrave.{macos_bundle_id(policy_file)}.pwa"


def macos_daemon_path(policy_file: Path) -> Path:
    """Absolute path to the LaunchDaemon plist for this browser."""
    return _MACOS_LAUNCHD_DIR / f"{macos_daemon_label(policy_file)}.plist"


def heal_script_source() -> Path:
    """Path to the packaged self-healing script.

    Shipped as a data file rather than an f-string because the darwin Nix
    module runs the same script; two copies of this shell would drift.
    """
    return Path(__file__).resolve().parent.parent / "data" / "heal.sh"


def build_heal_script(source_plist: Path, managed_plist: Path) -> str:
    """Self-contained shell script the daemon runs.

    Inlines the packaged data file's contents rather than ``exec``-ing its
    path, so the installed script keeps healing even if the dotbrave
    package that generated it is later upgraded, moved to a different
    venv, or uninstalled -- none of which should silently stop a
    root-owned daemon that already exists on disk.
    """
    heal_body = heal_script_source().read_text()
    # The data file carries its own `#!/bin/sh` for standalone use (e.g. by
    # the Nix module). This script already opens with one, so drop the
    # data file's copy rather than leave a second shebang sitting mid-file
    # as a stray comment.
    _, _, heal_body = heal_body.partition("\n")
    return (
        "#!/bin/sh\n"
        "# dotbrave self-healing PWA policy. Managed automatically; do not edit.\n"
        f'SRC="{source_plist}"\n'
        f'DEST="{managed_plist}"\n'
        f'LOG="{macos_heal_log(managed_plist)}"\n'
        f'BUNDLE="{macos_bundle_id(managed_plist)}"\n'
        # Exported for parity with the data file's own contract (it may run
        # standalone, e.g. from the Nix module) even though inlining here
        # already puts SRC/DEST/LOG/BUNDLE in the same shell.
        "export SRC DEST LOG BUNDLE\n"
        f"{heal_body}"
    )


def build_launchd_plist(label: str, heal_script: Path, watch_dir: str) -> bytes:
    """Serialize a WatchPaths LaunchDaemon plist that runs the heal script.

    macOS prunes plists it does not own from ``/Library/Managed Preferences``
    during boot, so the daemon is racing the browser's own login-item launch:
    a browser that starts first reads an empty policy, uninstalls every
    managed PWA, and never reloads the policy while running. ``ThrottleInterval``
    is therefore pinned to 1 -- launchd's default of 10 seconds is enough of a
    delay to lose that race -- and ``StartInterval`` re-checks periodically so
    a dropped WatchPaths notification still heals on its own. The heal script's
    ``cmp -s`` guard keeps both triggers cheap and loop-free.
    """
    payload = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", str(heal_script)],
        "WatchPaths": [watch_dir],
        "RunAtLoad": True,
        "ThrottleInterval": 1,
        "StartInterval": 60,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def _sudo_install_file(path: Path, content: bytes, mode: str) -> None:
    """Write ``content`` to ``path`` as a root-owned, ``mode`` file.

    Root ownership + non-world-writable modes stop a local user from
    editing the daemon, script, or policy source to force arbitrary
    browser PWAs (privilege escalation)."""
    subprocess.run(
        ["sudo", "tee", str(path)],
        input=content, stdout=subprocess.DEVNULL, check=True,
    )
    subprocess.run(["sudo", "chown", "root:wheel", str(path)], check=True)
    subprocess.run(["sudo", "chmod", mode, str(path)], check=True)


def _darwin_clear_immutable(path: Path) -> None:
    """Lift ``schg`` so a privileged writer can replace or delete ``path``."""
    subprocess.run(
        ["sudo", "chflags", "noschg", str(path)],
        check=False, stderr=subprocess.DEVNULL,
    )


def _darwin_set_immutable(path: Path) -> None:
    """Pin ``path`` against macOS's boot-time reconcile of the managed
    preferences directory, which unlinks plists no configuration profile
    claims."""
    subprocess.run(
        ["sudo", "chflags", "schg", str(path)],
        check=False, stderr=subprocess.DEVNULL,
    )


def _darwin_seed_daemon_source(policy_file: Path, managed_content: bytes) -> None:
    """Point the running daemon at the content we are about to write.

    The daemon restores anything that disagrees with its source plist, and at
    ``ThrottleInterval`` 1 it reacts well inside the window of our own write.
    Seeding first means a heal that fires mid-apply compares against the new
    content instead of restoring the policy we are replacing."""
    source_plist, _ = macos_support_paths(policy_file)
    subprocess.run(
        ["sudo", "mkdir", "-p", "-m", "0755", str(_MACOS_SUPPORT_DIR)],
        check=True,
    )
    _sudo_install_file(source_plist, managed_content, "0644")


def _darwin_stop_daemon(policy_file: Path) -> None:
    """Boot the daemon out. Used before emptying the policy, where there is no
    new content to seed and the daemon would restore what we are removing."""
    subprocess.run(
        ["sudo", "launchctl", "bootout", "system", str(macos_daemon_path(policy_file))],
        check=False, stderr=subprocess.DEVNULL,
    )


def install_self_healing_daemon(policy_file: Path, managed_content: bytes) -> None:
    """Install/refresh the self-healing daemon for this browser. Patchable
    in tests."""
    source_plist, heal_script = macos_support_paths(policy_file)
    daemon_path = macos_daemon_path(policy_file)
    label = macos_daemon_label(policy_file)
    script_text = build_heal_script(source_plist, policy_file)
    daemon_content = build_launchd_plist(label, heal_script, _MACOS_WATCH_DIR)

    subprocess.run(
        ["sudo", "mkdir", "-p", "-m", "0755", str(_MACOS_SUPPORT_DIR)],
        check=True,
    )
    _sudo_install_file(source_plist, managed_content, "0644")
    _sudo_install_file(heal_script, script_text.encode("utf-8"), "0755")
    _sudo_install_file(daemon_path, daemon_content, "0644")

    subprocess.run(
        ["sudo", "launchctl", "bootout", "system", str(daemon_path)],
        check=False, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sudo", "launchctl", "bootstrap", "system", str(daemon_path)],
        check=True,
    )


def remove_self_healing_daemon(policy_file: Path) -> None:
    """Unload and delete the daemon + its support files. Patchable in tests."""
    source_plist, heal_script = macos_support_paths(policy_file)
    daemon_path = macos_daemon_path(policy_file)
    subprocess.run(
        ["sudo", "launchctl", "bootout", "system", str(daemon_path)],
        check=False, stderr=subprocess.DEVNULL,
    )
    # Nothing is left to maintain the policy file, so it must not stay pinned:
    # an immutable orphan would outlive the uninstall.
    _darwin_clear_immutable(policy_file)
    for p in (daemon_path, heal_script, source_plist,
              macos_heal_log(policy_file)):
        subprocess.run(["sudo", "rm", "-f", str(p)], check=False)


# ---------------------------------------------------------------------------
# Linux launcher heal
#
# The managed policy applies to every --user-data-dir, and a launcher's file
# name (brave-<app-id>-<Profile>.desktop) does not include the data dir. So
# any Brave started on a throwaway profile -- a test harness, a headless
# screenshot -- force-installs the same apps and overwrites the real
# profile's launchers with its own --user-data-dir, silently sending every
# PWA into an empty profile. A systemd user path unit watches the
# applications dir and runs data/heal-launchers.sh on every change.
# ---------------------------------------------------------------------------

_LINUX_HEAL_UNIT = "dotbrave-pwa-heal"


@dataclass(frozen=True)
class LinuxHealPaths:
    """Where the Linux launcher heal lives. All per-user: unlike the macOS
    daemon it needs no root, because the launchers are the user's own files."""

    apps_dir: Path
    state_dir: Path
    unit_dir: Path

    @property
    def script(self) -> Path:
        return self.state_dir / "heal-launchers.sh"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "launchers"

    @property
    def log(self) -> Path:
        return self.state_dir / "heal-launchers.log"

    @property
    def path_unit(self) -> Path:
        return self.unit_dir / f"{_LINUX_HEAL_UNIT}.path"

    @property
    def service_unit(self) -> Path:
        return self.unit_dir / f"{_LINUX_HEAL_UNIT}.service"

    @property
    def timer_unit(self) -> Path:
        return self.unit_dir / f"{_LINUX_HEAL_UNIT}.timer"


def linux_heal_paths(env: Mapping[str, str] | None = None) -> LinuxHealPaths:
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())

    def xdg(var: str, default: str) -> Path:
        # The XDG spec says a relative value is invalid and must be ignored.
        value = env.get(var)
        return Path(value) if value and os.path.isabs(value) else home / default

    return LinuxHealPaths(
        apps_dir=xdg("XDG_DATA_HOME", ".local/share") / "applications",
        state_dir=xdg("XDG_STATE_HOME", ".local/state") / "dotbrave",
        unit_dir=xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user",
    )


def linux_heal_script_source() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "heal-launchers.sh"


def build_linux_heal_script(paths: LinuxHealPaths) -> str:
    """Inline the packaged script for the same reason as
    ``build_heal_script``: a unit already on disk must keep working after
    the package that wrote it is upgraded or garbage-collected."""
    _, _, body = linux_heal_script_source().read_text().partition("\n")
    return (
        "#!/bin/sh\n"
        f"APPS={shlex.quote(str(paths.apps_dir))}\n"
        f"SNAP={shlex.quote(str(paths.snapshot_dir))}\n"
        f"LOG={shlex.quote(str(paths.log))}\n"
        "export APPS SNAP LOG\n"
        f"{body}"
    )


def _unit_value(path: Path) -> str:
    # `%` starts a specifier in unit files.
    return str(path).replace("%", "%%")


def build_systemd_path_unit(paths: LinuxHealPaths) -> str:
    """``PathChanged=`` on a directory fires for a child file overwritten in
    place, copied over, rewritten by ``sed -i``, created or deleted --
    measured on systemd 261 -- so one watch covers however Brave writes its
    launchers."""
    return (
        "[Unit]\n"
        "Description=dotbrave: restore Brave PWA launchers a throwaway profile rewrote\n"
        "\n"
        "[Path]\n"
        f"PathChanged={_unit_value(paths.apps_dir)}\n"
        f"Unit={_LINUX_HEAL_UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_systemd_service_unit(paths: LinuxHealPaths) -> str:
    """``StartLimitIntervalSec=0`` is load-bearing: a throwaway profile
    writes a dozen launchers inside a few seconds, the default start limit
    (5 in 10s) failed the service with ``start-limit-hit``, and the path unit
    then failed too and stopped watching -- measured, with 12 launchers left
    hijacked."""
    return (
        "[Unit]\n"
        "Description=dotbrave: restore Brave PWA launchers a throwaway profile rewrote\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'ExecStart=/bin/sh "{_unit_value(paths.script)}"\n'
    )


def build_systemd_timer_unit() -> str:
    """Safety net, like the macOS daemon's ``StartInterval``: a launcher
    written while the service is already running fires no new trigger."""
    return (
        "[Unit]\n"
        "Description=dotbrave: periodic Brave PWA launcher heal\n"
        "\n"
        "[Timer]\n"
        "OnStartupSec=30\n"
        "OnUnitInactiveSec=60\n"
        f"Unit={_LINUX_HEAL_UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def linux_heal_files(paths: LinuxHealPaths) -> dict[Path, str]:
    return {
        paths.script: build_linux_heal_script(paths),
        paths.path_unit: build_systemd_path_unit(paths),
        paths.service_unit: build_systemd_service_unit(paths),
        paths.timer_unit: build_systemd_timer_unit(),
    }


def linux_heal_current(paths: LinuxHealPaths) -> bool:
    for path, text in linux_heal_files(paths).items():
        try:
            if path.read_text(encoding="utf-8") != text:
                return False
        except OSError:
            return False
    return True


def linux_heal_present(paths: LinuxHealPaths) -> bool:
    return any(p.exists() for p in linux_heal_files(paths))


def _systemctl_user(*args: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def install_linux_launcher_heal(paths: LinuxHealPaths) -> None:
    """Write the script and units, then start watching. Patchable in tests.

    The first run is started explicitly so the snapshot exists before any
    throwaway profile does."""
    for path, text in linux_heal_files(paths).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    paths.script.chmod(0o755)
    started = (
        _systemctl_user("daemon-reload")
        and _systemctl_user(
            "enable", "--now",
            f"{_LINUX_HEAL_UNIT}.path", f"{_LINUX_HEAL_UNIT}.timer",
        )
        and _systemctl_user("start", f"{_LINUX_HEAL_UNIT}.service")
    )
    if not started:
        print(
            "warning: [pwa] launcher heal is written but not running -- "
            "`systemctl --user` failed here. From a desktop session run: "
            f"systemctl --user enable --now {_LINUX_HEAL_UNIT}.path",
            file=sys.stderr,
        )


def remove_linux_launcher_heal(paths: LinuxHealPaths) -> None:
    """Stop watching and delete the heal's own files. Patchable in tests."""
    _systemctl_user(
        "disable", "--now", f"{_LINUX_HEAL_UNIT}.path", f"{_LINUX_HEAL_UNIT}.timer",
    )
    for path in (paths.path_unit, paths.service_unit, paths.timer_unit,
                 paths.script, paths.log):
        path.unlink(missing_ok=True)
    shutil.rmtree(paths.snapshot_dir, ignore_errors=True)
    try:
        paths.state_dir.rmdir()
    except OSError:
        pass
    _systemctl_user("daemon-reload")


@dataclass(frozen=True)
class LauncherHeal:
    """The Linux launcher heal as ``plan_apply`` sees it, injected by the
    browser wrapper so tests can fake it."""

    current: Callable[[], bool]
    present: Callable[[], bool]
    install: Callable[[], None]
    remove: Callable[[], None]


@dataclass
class PwaConfig:
    """Browser-specific PWA policy paths."""

    browser_name: str
    linux_policy_path: str
    macos_plist_path: str
    windows_registry_key: str
    sandbox_checks: list[tuple[str, str, str]] = field(default_factory=list)
    # Each sandbox check is (path_substring, install_name, policy_dir)


def default_policy_file(cfg: PwaConfig) -> Path | None:
    if sys.platform.startswith("linux"):
        return Path(cfg.linux_policy_path)
    if sys.platform == "darwin":
        return Path(cfg.macos_plist_path)
    return None


def check_platform_supported(policy_file: Path | None) -> None:
    if sys.platform == "win32":
        return
    if policy_file is None:
        sys.exit(
            f"error: [pwa] is not yet implemented on platform={sys.platform!r}. "
            f"Linux, macOS and Windows are supported."
        )


def check_install_supported(cfg: PwaConfig, prefs_path: Path) -> None:
    """Refuse [pwa] on sandboxed installs that can't read the policy dir."""
    p = str(prefs_path)
    for substr, install_name, policy_dir in cfg.sandbox_checks:
        if substr in p:
            sys.exit(
                f"error: [pwa] is not supported on {install_name} (the sandbox "
                f"does not read {policy_dir}). Install {cfg.browser_name.title()} "
                f"from the official package for [pwa] support, or "
                f"remove the [pwa] table from your config."
            )


def validate_table(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        sys.exit("error: [pwa] must be a table")
    extra = set(raw.keys()) - {"urls"}
    if extra:
        sys.exit(
            f"error: [pwa] has unsupported keys: {sorted(extra)}. "
            f"v1 only supports `urls = [...]`"
        )
    urls = raw.get("urls", [])
    if not isinstance(urls, list):
        sys.exit("error: [pwa] urls must be an array of strings")
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            sys.exit(f"error: [pwa] url entries must be strings, got {type(u).__name__}")
        if not u.startswith("https://"):
            sys.exit(f"error: [pwa] invalid url {u!r} (must start with https://)")
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def entry_for(url: str) -> dict[str, Any]:
    return {"url": url, **_DEFAULT_ENTRY}


def read_windows_registry_payload(windows_registry_key: str) -> dict:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            windows_registry_key + "\\" + POLICY_KEY,
            0,
            winreg.KEY_READ,
        )
    except OSError:
        return {}
    entries: list[dict] = []
    try:
        i = 0
        while True:
            try:
                _name, value, vtype = winreg.EnumValue(key, i)
                if vtype == winreg.REG_SZ and value:
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, dict):
                            entries.append(parsed)
                    except json.JSONDecodeError:
                        pass
                i += 1
            except OSError:
                break
    finally:
        winreg.CloseKey(key)
    return {POLICY_KEY: entries} if entries else {}


def read_existing_payload(
    policy_file: Path | None,
    windows_registry_key: str,
) -> dict:
    if sys.platform == "win32":
        return read_windows_registry_payload(windows_registry_key)
    if policy_file is None or not policy_file.exists():
        return {}
    try:
        if sys.platform == "darwin":
            with policy_file.open("rb") as f:
                data = plistlib.load(f)
        else:
            data = json.loads(policy_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, plistlib.InvalidFileException, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_current_policy(
    policy_file: Path | None,
    windows_registry_key: str,
) -> dict[str, dict]:
    data = read_existing_payload(policy_file, windows_registry_key)
    entries = data.get(POLICY_KEY, [])
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict] = {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("url"), str):
            out[e["url"]] = e
    return out


def build_policy_payload(
    policy_file: Path | None,
    windows_registry_key: str,
    entries: list[dict],
) -> bytes:
    if sys.platform == "darwin":
        merged = dict(read_existing_payload(policy_file, windows_registry_key))
        merged[POLICY_KEY] = entries
        return plistlib.dumps(merged, fmt=plistlib.FMT_BINARY)
    payload = {POLICY_KEY: entries}
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def diff_summary(current: dict[str, dict], target_urls: list[str]) -> list[str]:
    target_set = set(target_urls)
    current_set = set(current)
    lines: list[str] = []
    for url in sorted(target_set - current_set):
        lines.append(f"  + {url}")
    for url in sorted(current_set - target_set):
        lines.append(f"  - {url} (uninstall)")
    return lines


def write_windows_registry(
    windows_registry_key: str,
    entries: list[dict],
) -> None:
    key_path = windows_registry_key + "\\" + POLICY_KEY
    parent = winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE,
        windows_registry_key,
        0,
        winreg.KEY_WRITE,
    )
    winreg.CloseKey(parent)
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key_path)
    except FileNotFoundError:
        pass
    key = winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE,
        key_path,
        0,
        winreg.KEY_WRITE,
    )
    try:
        for i, entry_item in enumerate(entries, start=1):
            winreg.SetValueEx(key, str(i), 0, winreg.REG_SZ, json.dumps(entry_item))
    finally:
        winreg.CloseKey(key)


def sudo_write_policy(
    policy_file: Path | None,
    windows_registry_key: str,
    entries: list[dict],
) -> None:
    """Write policy entries via the platform-specific privileged path."""
    if sys.platform == "win32":
        write_windows_registry(windows_registry_key, entries)
        return
    content = build_policy_payload(policy_file, windows_registry_key, entries)
    subprocess.run(
        ["sudo", "mkdir", "-p", "-m", "0755", str(policy_file.parent)],
        check=True,
    )
    if sys.platform == "darwin":
        if entries:
            _darwin_seed_daemon_source(policy_file, content)
        else:
            _darwin_stop_daemon(policy_file)
        _darwin_clear_immutable(policy_file)
    subprocess.run(
        ["sudo", "tee", str(policy_file)],
        input=content,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    if sys.platform == "darwin":
        if entries:
            _darwin_set_immutable(policy_file)
        subprocess.run(
            ["sudo", "killall", "cfprefsd"],
            check=False,
            stderr=subprocess.DEVNULL,
        )
        if entries:
            install_self_healing_daemon(policy_file, content)
        else:
            remove_self_healing_daemon(policy_file)


def plan_apply(
    cfg: PwaConfig,
    policy_file: Path | None,
    sudo_write_fn,
    read_policy_fn,
    prefs_path: Path,
    prefs: dict,
    raw_table: object,
    launcher_heal: LauncherHeal | None = None,
) -> Plan:
    """Build a Plan for the [pwa] table.

    ``policy_file``, ``sudo_write_fn``, and ``read_policy_fn`` are
    passed in by the browser wrapper so that tests can monkeypatch
    the browser module's attributes and the changes are visible here.

    ``launcher_heal`` is diffed separately from the policy, so a machine
    whose policy already matches still gets the heal installed -- and that
    run writes no policy, so it does not need sudo for anything.
    """
    check_platform_supported(policy_file)
    check_install_supported(cfg, prefs_path)

    target_urls = validate_table(raw_table)
    current = read_policy_fn()

    diff = diff_summary(current, target_urls)
    policy_dirty = bool(diff)

    heal_action = None
    if launcher_heal is not None:
        if target_urls and not launcher_heal.current():
            heal_action = launcher_heal.install
            diff.append(f"  + launcher heal (systemd --user {_LINUX_HEAL_UNIT}.path)")
        elif not target_urls and launcher_heal.present():
            heal_action = launcher_heal.remove
            diff.append(f"  - launcher heal (systemd --user {_LINUX_HEAL_UNIT}.path)")

    def apply_fn(_prefs: dict) -> None:
        pass

    def verify_fn(_reloaded: dict) -> None:
        pass

    def external_apply_fn() -> None:
        if policy_dirty:
            entries = [entry_for(u) for u in target_urls]
            sudo_write_fn(entries)
            actual = read_policy_fn()
            if set(actual) != set(target_urls):
                sys.exit(
                    "error: pwa verification failed: policy file URL set does "
                    f"not match config (wrote {sorted(target_urls)}, "
                    f"file has {sorted(actual)})"
                )
        if heal_action is not None:
            heal_action()

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        external_apply_fn=external_apply_fn,
    )


def build_dump_block(
    policy_file: Path | None,
    windows_registry_key: str,
    read_policy_fn,
    *,
    header_comment: str | None = None,
) -> list[str]:
    """Pure builder for the `[pwa]` TOML block.

    Reads the current managed-policy file (or registry on Windows) and
    returns the lines as a list (no trailing newline) so callers can
    decide how to join them.  Used by both ``cmd_dump`` and the unified
    ``<browser> export`` command.
    """
    current = read_policy_fn()
    urls = sorted(current)

    lines: list[str] = []
    if header_comment is not None:
        lines.append(header_comment)
    lines.append("[pwa]")
    if urls:
        lines.append("urls = [")
        for u in urls:
            lines.append(f"  {json.dumps(u)},")
        lines.append("]")
    else:
        lines.append("urls = []")
        lines.append("")
        if sys.platform == "win32":
            location = f"HKLM\\{windows_registry_key}\\{POLICY_KEY}"
        else:
            location = str(policy_file)
        lines.append(f"# (no managed PWAs -- {location} does not exist or is empty)")
    return lines


def cmd_dump(
    browser_name: str,
    policy_file: Path | None,
    windows_registry_key: str,
    read_policy_fn,
    args: argparse.Namespace,
) -> None:
    check_platform_supported(policy_file)
    find_preferences(args.profile_root, args.profile)

    header = "# Generated by `dotbrave export`"
    lines = build_dump_block(
        policy_file, windows_registry_key, read_policy_fn,
        header_comment=header,
    )
    out = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(out)


def register(
    browser_name: str,
    policy_file: Path | None,
    windows_registry_key: str,
    read_policy_fn,
    cmd_dump_fn,
    subparsers: argparse._SubParsersAction,
    profile_args,
) -> None:
    p = subparsers.add_parser(
        "pwa",
        help="inspect force-installed PWAs (apply lives at `dotbrave apply`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Inspect force-installed PWAs managed through {browser_name.title()} policy.

The [pwa] table controls Chromium's managed policy list of HTTPS app URLs.
Writing changes occurs through `dotbrave apply` and requires elevated
privileges; `dump` is read-only.""",
        epilog="""\
Example:
  dotbrave export -o pwa.toml""",
    )
    sub = p.add_subparsers(dest="action", required=True, metavar="ACTION")

    if sys.platform == "win32":
        _help_path = f"HKLM\\{windows_registry_key}"
    else:
        _help_path = policy_file or "the managed-policy file"
    d = sub.add_parser(
        "dump",
        help=f"emit URLs from {_help_path} as a `[pwa]` TOML table",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Emit a [pwa] TOML table from the current managed policy source.

Source:
  {_help_path}

This is a read-only operation and does not need the privileges required by
`dotbrave apply` when policy changes are written.""",
        epilog="""\
Example:
  dotbrave export -o pwa.toml""",
    )
    profile_args(d)
    d.add_argument("-o", "--output", help="write to file instead of stdout")
    d.set_defaults(func=cmd_dump_fn)
