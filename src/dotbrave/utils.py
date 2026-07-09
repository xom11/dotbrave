"""Brave-specific process config + re-exports of shared utilities.

This module is the public API that ``brave/shortcuts.py``,
``brave/settings.py``, and ``brave/pwa.py`` import from.  It re-exports
the shared utilities from ``_base`` so existing imports keep working.
"""
from __future__ import annotations

# Re-export shared utilities (used by shortcuts.py, settings.py, pwa.py)
from dotbrave._base.utils import (  # noqa: F401
    Plan,
    find_preferences,
    get_nested,
    load_prefs,
    write_atomic,
)
from dotbrave._base.process import BrowserProcess

# ---------------------------------------------------------------------------
# Brave-specific process configuration
# ---------------------------------------------------------------------------

_CHANNEL_PRETTY = {"stable": "", "beta": " Beta", "nightly": " Nightly"}
_CHANNEL_DIR = {"stable": "", "beta": "-Beta", "nightly": "-Nightly"}
_CHANNEL_LOWER = {"stable": "", "beta": "-beta", "nightly": "-nightly"}


def _make_browser_process(channel: str = "stable") -> BrowserProcess:
    """BrowserProcess configured for a Brave release channel.

    Linux note: all channels share the inner binary basename ``brave``
    (each channel installs to ``/opt/brave.com/brave{,-beta,-nightly}/``
    but the executable inside is always ``brave``).  ``pgrep -x brave``
    on Linux can't distinguish channels by name, so for non-stable
    channels we narrow the pid set with ``linux_pid_filter`` -- pids
    whose argv[0] doesn't contain ``/opt/brave.com/brave-{channel}/``
    are dropped before we ``pkill``, so a beta apply doesn't kill the
    user's running stable Brave (and vice versa).  Stable keeps the
    permissive behavior because Snap/Flatpak installs use other paths
    (``/snap/brave/...`` / ``/app/brave/...``) that a filter would
    falsely exclude.  macOS and Windows have channel-distinct names
    already (``Brave Browser Beta`` and
    ``Brave-Browser-Beta\\Application\\brave.exe`` respectively).
    """
    pretty = _CHANNEL_PRETTY[channel]
    dir_suf = _CHANNEL_DIR[channel]
    lower = _CHANNEL_LOWER[channel]
    is_stable = channel == "stable"
    return BrowserProcess(
        display_name=f"Brave{pretty}",
        proc_name_linux="brave",
        proc_name_macos=f"Brave Browser{pretty}",
        proc_name_windows="brave.exe",
        macos_app_name=f"Brave Browser{pretty}",
        linux_wrappers=(
            ["brave-browser", "brave"]
            if is_stable
            else [f"brave-browser{lower}", f"brave{lower}"]
        ),
        windows_exe_relpath=(
            "BraveSoftware", f"Brave-Browser{dir_suf}", "Application", "brave.exe",
        ),
        flatpak_prefix="/app/brave/" if is_stable else None,
        flatpak_app_id="com.brave.Browser" if is_stable else None,
        linux_pid_filter=(
            None if is_stable else f"/opt/brave.com/brave{lower}/"
        ),
    )


BROWSER_PROCESS = _make_browser_process("stable")


# ---------------------------------------------------------------------------
# Backward-compatible function aliases (used by brave/__init__.py and tests)
# ---------------------------------------------------------------------------

def _brave_proc_name() -> str:
    return BROWSER_PROCESS.proc_name()


def brave_running() -> bool:
    return BROWSER_PROCESS.running()


def _brave_pids() -> list[str]:
    return BROWSER_PROCESS.pids()


def find_main_brave_cmdline() -> list[str] | None:
    return BROWSER_PROCESS.find_main_cmdline()


def kill_brave_and_wait(timeout: float = 5.0) -> None:
    BROWSER_PROCESS.kill_and_wait(timeout)


def restart_brave(captured_cmdline: list[str]) -> list[str]:
    return BROWSER_PROCESS.restart(captured_cmdline)


def _is_flatpak_brave_cmdline(captured_cmdline: list[str]) -> bool:
    return BROWSER_PROCESS._is_flatpak_cmdline(captured_cmdline)
