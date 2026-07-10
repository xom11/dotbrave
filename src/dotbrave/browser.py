"""Brave-browser-specific subcommands.

Top-level CLI shape:

    dotbrave [--profile-root ...] [--profile ...] <ACTION> ...

Where <ACTION> is one of exactly two verbs:
- ``export [-o FILE] [-a] [--snapshot [--clear]]`` -- read the current
  ``[shortcuts]`` + ``[settings]`` + ``[pwa]`` customizations as TOML
  (or capture/clear the settings baseline).
- ``apply <file> | --undo`` -- write a TOML config back to Brave, or
  restore the most recent apply-time Preferences backup.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotbrave._base.orchestrator import (
    cmd_apply as _base_cmd_apply,
    cmd_export as _base_cmd_export,
    cmd_restore as _base_cmd_restore,
    register_actions,
)
from dotbrave._base.utils import Plan
from dotbrave import live as live_mod
from dotbrave import pwa as pwa_mod
from dotbrave import settings as settings_mod
from dotbrave import shortcuts as shortcuts_mod
from dotbrave.utils import (  # noqa: F401
    BROWSER_PROCESS,
    brave_running,
    find_main_brave_cmdline,
    restart_brave,
)


CHANNELS = ("stable", "beta", "nightly")

# Path-suffix Brave appends to the Brave-Browser directory name for
# beta/nightly channels.  Same on every OS.
_CHANNEL_DIR_SUFFIX = {"stable": "", "beta": "-Beta", "nightly": "-Nightly"}


def _default_profile_root(channel: str = "stable") -> Path | None:
    """Brave's profile root, per platform and channel.

    Returns None for unsupported platforms; the CLI then requires
    --profile-root to be passed explicitly so that --help still works
    on BSD / etc. without crashing at import time.

    Snap and Flatpak only ship a stable channel (verified against
    Brave's official packaging), so non-stable channels probe only
    the direct-install path.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    suffix = _CHANNEL_DIR_SUFFIX[channel]
    home = Path.home()
    if sys.platform == "darwin":
        return (
            home / "Library" / "Application Support" / "BraveSoftware"
            / f"Brave-Browser{suffix}"
        )
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = (
                Path(local_app_data) / "BraveSoftware"
                / f"Brave-Browser{suffix}" / "User Data"
            )
            return candidate
        return None
    if sys.platform.startswith("linux"):
        direct = home / ".config" / "BraveSoftware" / f"Brave-Browser{suffix}"
        if channel != "stable":
            # Snap/Flatpak only ship stable.
            return direct
        candidates = (
            direct,
            home / "snap" / "brave" / "current" / ".config" / "BraveSoftware" / "Brave-Browser",
            home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser",
        )
        for c in candidates:
            if (c / "Local State").exists():
                return c
        return candidates[0]
    return None


DEFAULT_PROFILE_ROOT = _default_profile_root()


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _build_plans(prefs_path: Path, prefs: dict, doc: dict) -> list[Plan]:
    plans: list[Plan] = []
    if shortcuts_mod.NAMESPACE in doc:
        plans.append(
            shortcuts_mod.plan_apply(prefs_path, prefs, doc[shortcuts_mod.NAMESPACE])
        )
    if settings_mod.NAMESPACE in doc:
        plans.append(
            settings_mod.plan_apply(prefs_path, prefs, doc[settings_mod.NAMESPACE])
        )
    if pwa_mod.NAMESPACE in doc:
        plans.append(
            pwa_mod.plan_apply(prefs_path, prefs, doc[pwa_mod.NAMESPACE])
        )
    return plans


# ---------------------------------------------------------------------------
# Channel-aware argument resolution
# ---------------------------------------------------------------------------

def _setup_brave_profile_args(
    parser: argparse.ArgumentParser, *, leaf: bool = False
) -> None:
    """Brave-specific profile flags, attachable at two levels.

    The root parser carries the real defaults so flags may be given
    before the action name. Profile-reading action ("leaf") parsers
    re-declare the same flags with ``argparse.SUPPRESS`` defaults, so
    `dotbrave apply --channel beta cfg.toml` overrides the root value
    instead of resetting it to the default. The default for
    ``--profile-root`` is deferred to runtime
    (``_normalize_brave_args``) because it depends on which channel
    the user picked.
    """

    def default(value):
        return argparse.SUPPRESS if leaf else value

    parser.add_argument(
        "--channel",
        choices=CHANNELS,
        default=default("stable"),
        help="Brave release channel (default: stable)",
    )
    parser.add_argument(
        "-r",
        "--profile-root",
        type=Path,
        default=default(None),
        help="default: auto-detect from --channel",
    )
    parser.add_argument(
        "-p",
        "--profile",
        default=default("Default"),
        help="profile dir name (default: Default)",
    )
    if leaf:
        parser.set_defaults(_needs_profile=True)


def _normalize_brave_args(args: argparse.Namespace) -> None:
    """Fill in ``args.profile_root`` from ``args.channel`` when omitted.

    Only actions that read a profile carry the ``_needs_profile``
    marker; the rest (``init``, ``shortcuts list``) skip resolution so
    they keep working on platforms without a known Brave profile root.
    """
    if not getattr(args, "_needs_profile", False):
        return
    if getattr(args, "profile_root", None) is None:
        root = _default_profile_root(args.channel)
        if root is None:
            sys.exit(
                f"error: no default Brave profile root for platform "
                f"{sys.platform!r} (channel={args.channel!r}); "
                f"pass --profile-root explicitly"
            )
        args.profile_root = root


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def cmd_apply(args: argparse.Namespace) -> None:
    """Unified apply for Brave.

    ``--undo`` routes to the restore engine (most recent Preferences
    backup).  For ``--channel stable`` we keep the module-level callbacks
    so tests can monkeypatch ``brave_pkg.brave_running`` etc.  For
    beta/nightly we use a freshly built BrowserProcess (those channels
    have no test coverage today).
    """
    if getattr(args, "undo", False):
        if getattr(args, "config", None):
            sys.exit("error: --undo takes no CONFIG argument")
        args.from_path = None
        args.list = False
        cmd_restore(args)
        return
    if not getattr(args, "config", None):
        sys.exit("error: CONFIG is required (or pass --undo)")

    channel = getattr(args, "channel", "stable")
    if channel == "stable":
        _base_cmd_apply(
            args,
            display_name="Brave",
            running_fn=brave_running,
            find_cmdline_fn=find_main_brave_cmdline,
            restart_fn=restart_brave,
            build_plans_fn=_build_plans,
            live_apply_fn=live_mod.apply_live,
            graceful_close_fn=BROWSER_PROCESS.close_and_wait,
            launch_live_fn=BROWSER_PROCESS.launch_live,
        )
        return

    from dotbrave.utils import _make_browser_process
    proc = _make_browser_process(channel)
    _base_cmd_apply(
        args,
        display_name=proc.display_name,
        running_fn=proc.running,
        find_cmdline_fn=proc.find_main_cmdline,
        restart_fn=proc.restart,
        build_plans_fn=_build_plans,
        live_apply_fn=live_mod.apply_live,
        graceful_close_fn=proc.close_and_wait,
        launch_live_fn=proc.launch_live,
    )


def _export_shortcuts(args: argparse.Namespace, prefs_path: Path, prefs: dict) -> list[str]:
    return shortcuts_mod.build_dump_block(
        prefs, all_bindings=getattr(args, "all_shortcuts", False)
    )


def _export_settings(
    args: argparse.Namespace, prefs_path: Path, prefs: dict
) -> list[str] | None:
    return settings_mod.build_export_lines(args, prefs_path, prefs)


def _export_pwa(args: argparse.Namespace, prefs_path: Path, prefs: dict) -> list[str] | None:
    if pwa_mod.POLICY_FILE is None and sys.platform != "win32":
        return None
    return pwa_mod.build_dump_block()


def cmd_export(args: argparse.Namespace) -> None:
    if getattr(args, "snapshot", False) or getattr(args, "clear", False):
        if not getattr(args, "snapshot", False):
            sys.exit("error: --clear requires --snapshot")
        settings_mod.cmd_snapshot(args)
        return
    _base_cmd_export(
        args,
        browser_name="brave",
        builders=[_export_shortcuts, _export_settings, _export_pwa],
    )


def cmd_restore(args: argparse.Namespace) -> None:
    """Restore Preferences from an apply-time backup.

    Resolves process callbacks the same way ``cmd_apply`` does so the
    Linux non-stable channel filter (and macOS app-name distinction)
    are honored when killing the right Brave install.
    """
    channel = getattr(args, "channel", "stable")
    if channel == "stable":
        _base_cmd_restore(
            args,
            display_name="Brave",
            running_fn=brave_running,
            find_cmdline_fn=find_main_brave_cmdline,
            restart_fn=restart_brave,
            graceful_close_fn=BROWSER_PROCESS.close_and_wait,
        )
        return

    from dotbrave.utils import _make_browser_process
    proc = _make_browser_process(channel)
    _base_cmd_restore(
        args,
        display_name=proc.display_name,
        running_fn=proc.running,
        find_cmdline_fn=proc.find_main_cmdline,
        restart_fn=proc.restart,
        graceful_close_fn=proc.close_and_wait,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(parser: argparse.ArgumentParser) -> None:
    register_actions(
        parser,
        display_name="Brave",
        namespaces=("shortcuts", "settings", "pwa"),
        cmd_apply_fn=cmd_apply,
        cmd_export_fn=cmd_export,
        export_has_shortcuts=True,
        module_registers=[],
        setup_profile_args=_setup_brave_profile_args,
        normalize_args=_normalize_brave_args,
    )
