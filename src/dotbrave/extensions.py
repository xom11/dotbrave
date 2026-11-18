"""Manage unpacked Brave extensions and their shortcuts -- the ``[extensions]`` table.

Why unpacked extensions need CDP rather than direct JSON edits:
- ``extensions.settings`` and ``extensions.ui.developer_mode`` are tracked in
  ``Secure Preferences`` under ``ENFORCE_ON_LOAD`` on macOS and Windows
  (``chrome_pref_service_factory.cc``), so entries written outside Brave are
  wiped on startup.
- Enterprise policies ``ExtensionInstallForcelist`` / ``ExtensionSettings`` are
  filtered with ``[BLOCKED]`` for off-Web-Store ``update_url``s by
  ``FilterSensitivePolicies`` (``policy_loader_common.cc``) on non-MDM macOS
  and non-AD Windows.
- Plain Browser-target CDP ``Extensions.loadUnpacked`` works on a standard
  ``--remote-debugging-port`` endpoint without extra flags, but stamps
  ``Extension::INSTALLED_VIA_CDP`` (``1 << 15``) onto ``creation_flags``, which
  ``ExtensionPrefs::CleanUpCdpInstalledExtensions()`` deletes on the next
  browser startup.
- Immediately following ``Extensions.loadUnpacked`` with
  ``chrome.developerPrivate.reload(id, {failQuietly: true})`` in a background
  ``chrome://extensions`` work tab runs ``UnpackedInstaller::Create(profile)``
  with ``installed_via_cdp_ == false``, rewriting ``creation_flags`` without
  that bit into ``Secure Preferences`` with Brave's own MAC so the unpacked
  extension survives every restart.
- ``extensions.commands`` lives in plain ``Preferences`` (unprotected by
  ``Secure Preferences``) and can be updated both live through
  ``chrome.developerPrivate.updateExtensionCommand`` (bypassing
  ``CommandService::CanAutoAssign``'s static default-accelerator check on
  Windows/Linux) and offline in ``Preferences``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotbrave import pwa as pwa_mod
from dotbrave._base import pwa as _base_pwa
from dotbrave._base.cdp import (
    CdpClient,
    CdpError,
    discover_browser_ws_path,
    pick_unused_port,
    wait_for_devtools_endpoint,
)
from dotbrave._base.utils import Plan, get_nested, retry_on_permission_error

NAMESPACE = "extensions"
_UNPACKED_LOCATION = 4
_INSTALLED_VIA_CDP_FLAG = 1 << 15
_EXTENSIONS_URL = "chrome://extensions"
_IGNORED_NAMES = frozenset({".git", "__pycache__", ".DS_Store", "Thumbs.db"})

_MEDIA_KEYS = frozenset({
    "MediaNextTrack",
    "MediaPlayPause",
    "MediaPrevTrack",
    "MediaStop",
})

_SPECIAL_KEYS: dict[str, str] = {
    "comma": "Comma",
    ",": "Comma",
    "period": "Period",
    ".": "Period",
    "arrowup": "Up",
    "up": "Up",
    "arrowdown": "Down",
    "down": "Down",
    "arrowleft": "Left",
    "left": "Left",
    "arrowright": "Right",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "insert": "Insert",
    "ins": "Insert",
    "delete": "Delete",
    "del": "Delete",
    "space": "Space",
    "tab": "Tab",
    "medianexttrack": "MediaNextTrack",
    "mediaplaypause": "MediaPlayPause",
    "mediaprevtrack": "MediaPrevTrack",
    "mediastop": "MediaStop",
}

_REVERSE_SPECIAL_KEYS: dict[str, str] = {
    "Up": "ArrowUp",
    "Down": "ArrowDown",
    "Left": "ArrowLeft",
    "Right": "ArrowRight",
}


@dataclass(frozen=True)
class UnpackedEntry:
    path: Path
    display_path: str
    ext_id: str
    source_sha256: str
    shortcuts: dict[str, str] = field(default_factory=dict)
    incognito: bool | None = None


@dataclass(frozen=True)
class ExtensionEntryOp:
    entry: UnpackedEntry
    needs_install: bool
    needs_reload: bool
    needs_incognito_update: bool
    shortcut_writes: dict[str, str]


@dataclass(frozen=True)
class ExtensionPlanOps:
    entry_ops: list[ExtensionEntryOp]
    uninstall_ids: list[str]

    @property
    def needs_browser_cdp(self) -> bool:
        if self.uninstall_ids:
            return True
        return any(
            op.needs_install or op.needs_reload or op.needs_incognito_update
            for op in self.entry_ops
        )


def _command_platform_key(plat: str | None = None) -> str:
    """Chromium ``Command::CommandPlatform()`` prefix in ``extensions.commands``."""
    p = sys.platform if plat is None else plat
    if p == "darwin" or p == "macos":
        return "mac"
    if p == "win32" or p == "windows":
        return "windows"
    return "linux"


def _encode_id_digest(digest: bytes) -> str:
    return "".join(
        chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0x0F))
        for b in digest[:16]
    )


def unpacked_extension_id(path: Path | str, *, platform: str | None = None) -> str:
    """Compute Chromium's ``crx_file::id_util::GenerateIdForPath`` for ``path``.

    POSIX hashes ``FilePath::value()`` bytes (UTF-8 / fsencode); Windows
    uppercases an ASCII drive letter and hashes ``std::wstring`` UTF-16LE
    bytes without a ``\\\\?\\`` extended-length prefix.
    """
    plat = sys.platform if platform is None else platform
    raw_str = str(path)
    if plat in ("win32", "windows"):
        if raw_str.startswith("\\\\?\\") and not raw_str.startswith("\\\\?\\UNC\\"):
            raw_str = raw_str[4:]
        if len(raw_str) >= 2 and "a" <= raw_str[0] <= "z" and raw_str[1] == ":":
            raw_str = raw_str[0].upper() + raw_str[1:]
        data = raw_str.encode("utf-16-le")
    else:
        data = os.fsencode(raw_str)
    return _encode_id_digest(hashlib.sha256(data).digest())


def hash_extension_dir(path: Path) -> str:
    """Deterministic SHA-256 of every regular file under ``path``."""
    h = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for root, dirs, filenames in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_NAMES)
        root_path = Path(root)
        for fname in sorted(filenames):
            if fname in _IGNORED_NAMES:
                continue
            fpath = root_path / fname
            if fpath.is_file():
                rel = fpath.relative_to(path).as_posix()
                files.append((rel, fpath))
    files.sort(key=lambda item: item[0])
    for rel, fpath in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(fpath.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _canonical_platform_name(plat: str | None = None) -> str | None:
    p = sys.platform if plat is None else plat
    if p in ("linux", "macos", "windows"):
        return p
    return _base_pwa.current_platform_name(p)


def normalize_extension_shortcut(raw: str, *, platform: str | None = None) -> str:
    """Normalize a KeyEvent or Chromium command shortcut string into the
    canonical form ``Command::AcceleratorToString`` writes (`Ctrl+B`,
    `Command+Shift+Y`, `Alt+Up`, or ``""`` to unbind)."""
    if not isinstance(raw, str):
        sys.exit(f"error: [extensions] shortcut must be a string, got {raw!r}")
    if raw == "":
        return ""
    if raw != raw.strip() or not raw:
        sys.exit(
            f"error: [extensions] invalid shortcut {raw!r} "
            f"(surrounding whitespace is not allowed)"
        )

    plat_name = _canonical_platform_name(platform)
    tokens = [t for t in raw.split("+") if t]
    if len(tokens) != raw.count("+") + 1:
        sys.exit(f"error: [extensions] malformed shortcut {raw!r}")

    ctrl = alt = shift = cmd = False
    key_tokens: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in ("control", "ctrl", "macctrl"):
            ctrl = True
        elif low in ("alt", "option"):
            alt = True
        elif low == "shift":
            shift = True
        elif low in ("meta", "command", "cmd"):
            if plat_name is not None and plat_name != "macos":
                sys.exit(
                    f"error: [extensions] shortcut {raw!r} uses {tok}+, which "
                    f"Chromium extension shortcuts only support on macOS"
                )
            cmd = True
        else:
            key_tokens.append(tok)

    if len(key_tokens) != 1:
        sys.exit(
            f"error: [extensions] shortcut {raw!r} must name exactly one non-modifier key"
        )
    raw_key = key_tokens[0]
    low_key = raw_key.lower()

    if len(raw_key) == 4 and low_key.startswith("key") and raw_key[3].isalpha():
        canon_key = raw_key[3].upper()
    elif len(raw_key) == 1 and raw_key.isalpha():
        canon_key = raw_key.upper()
    elif len(raw_key) == 6 and low_key.startswith("digit") and raw_key[5].isdigit():
        canon_key = raw_key[5]
    elif len(raw_key) == 1 and raw_key.isdigit():
        canon_key = raw_key
    elif low_key in _SPECIAL_KEYS:
        canon_key = _SPECIAL_KEYS[low_key]
    else:
        sys.exit(
            f"error: [extensions] unsupported key {raw_key!r} in shortcut {raw!r}"
        )

    if canon_key in _MEDIA_KEYS:
        if ctrl or alt or shift or cmd:
            sys.exit(
                f"error: [extensions] media key {raw!r} must not include modifiers"
            )
        return canon_key

    if not (ctrl or alt or cmd):
        sys.exit(
            f"error: [extensions] shortcut {raw!r} requires Control, Alt"
            + (" or Meta/Command" if plat_name in (None, "macos") else "")
        )

    parts: list[str] = []
    if ctrl:
        parts.append("Ctrl")
    if alt:
        parts.append("Alt")
    if cmd:
        parts.append("Command")
    if shift:
        parts.append("Shift")
    parts.append(canon_key)
    return "+".join(parts)


def to_keyevent_shortcut(canon: str) -> str:
    """Convert a canonical ``extensions.commands`` shortcut (`Ctrl+B`) to
    portable KeyEvent form (`Control+KeyB`) for ``dotbrave export``."""
    if not canon:
        return ""
    tokens = canon.split("+")
    out: list[str] = []
    for tok in tokens[:-1]:
        if tok in ("Ctrl", "MacCtrl"):
            out.append("Control")
        elif tok == "Command":
            out.append("Meta")
        else:
            out.append(tok)
    key = tokens[-1]
    if len(key) == 1 and key.isalpha():
        out.append(f"Key{key.upper()}")
    elif len(key) == 1 and key.isdigit():
        out.append(f"Digit{key}")
    elif key in _REVERSE_SPECIAL_KEYS:
        out.append(_REVERSE_SPECIAL_KEYS[key])
    else:
        out.append(key)
    return "+".join(out)


def _resolve_extension_path(raw_path: str, config_dir: Path | None) -> Path:
    expanded = Path(os.path.expanduser(raw_path))
    if not expanded.is_absolute():
        if config_dir is None:
            sys.exit(
                f"error: [extensions] relative path {raw_path!r} requires a "
                f"local TOML file, not a remote URL"
            )
        expanded = config_dir / expanded
    try:
        return expanded.resolve()
    except OSError as e:
        sys.exit(f"error: [extensions] cannot resolve {raw_path!r}: {e}")


def _validate_manifest_dir(path: Path, display_path: str) -> None:
    if not path.is_dir():
        sys.exit(
            f"error: [extensions] unpacked directory not found: {path} "
            f"(from {display_path!r})"
        )
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        sys.exit(
            f"error: [extensions] manifest.json not found in {path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(
            f"error: [extensions] invalid manifest.json in {path}: {e}"
        )
    if not isinstance(manifest, dict):
        sys.exit(
            f"error: [extensions] manifest.json in {path} must be a JSON object"
        )


def validate_table(
    raw: object,
    *,
    config_dir: Path | None = None,
    platform: str | None = _base_pwa._AUTO_PLATFORM,
) -> list[UnpackedEntry]:
    if not isinstance(raw, dict):
        sys.exit("error: [extensions] must be a table")
    extra = set(raw) - {"unpacked"}
    if extra:
        sys.exit(
            f"error: [extensions] has unsupported keys: {sorted(extra)}. "
            f"Only `unpacked = [...]` is supported"
        )
    items = raw.get("unpacked", [])
    if not isinstance(items, list):
        sys.exit("error: [extensions] unpacked must be an array")

    resolved_platform = (
        _base_pwa.current_platform_name()
        if platform == _base_pwa._AUTO_PLATFORM
        else platform
    )
    if (
        resolved_platform is not None
        and resolved_platform not in _base_pwa.VALID_PLATFORMS
    ):
        sys.exit(
            f"error: unknown target platform {resolved_platform!r} "
            f"(expected one of {list(_base_pwa.VALID_PLATFORMS)})"
        )

    seen_ids: set[str] = set()
    out: list[UnpackedEntry] = []
    for item in items:
        raw_shortcuts: dict[str, Any] = {}
        incognito: bool | None = None
        entry_platforms: list[str] | None = None

        if isinstance(item, str):
            raw_path = item
        elif isinstance(item, dict):
            bad = set(item) - {"path", "shortcuts", "incognito", "platforms"}
            if bad:
                sys.exit(
                    f"error: [extensions] entry has unsupported keys: {sorted(bad)} "
                    f'(expected {{ path = "...", shortcuts = {{...}}, '
                    f"incognito = bool, platforms = [...] }})"
                )
            raw_path = item.get("path")
            if not isinstance(raw_path, str):
                sys.exit(
                    'error: [extensions] table entry needs a string `path = "..."`'
                )
            if "incognito" in item:
                if not isinstance(item["incognito"], bool):
                    sys.exit(
                        f"error: [extensions] incognito must be a boolean, "
                        f"got {item['incognito']!r}"
                    )
                incognito = item["incognito"]
            if "platforms" in item:
                plats = item["platforms"]
                if not isinstance(plats, list) or not plats:
                    sys.exit(
                        f"error: [extensions] platforms must be a non-empty array "
                        f"of {list(_base_pwa.VALID_PLATFORMS)}, got {plats!r}"
                    )
                for p_item in plats:
                    if not isinstance(p_item, str) or p_item not in _base_pwa.VALID_PLATFORMS:
                        sys.exit(
                            f"error: [extensions] invalid platform {p_item!r} "
                            f"(expected one of {list(_base_pwa.VALID_PLATFORMS)})"
                        )
                entry_platforms = plats
            if "shortcuts" in item:
                sc = item["shortcuts"]
                if not isinstance(sc, dict):
                    sys.exit("error: [extensions] shortcuts must be a table")
                raw_shortcuts = sc
        else:
            sys.exit(
                f"error: [extensions] unpacked entries must be strings or tables, "
                f"got {type(item).__name__}"
            )

        if not raw_path.strip() or raw_path != raw_path.strip():
            sys.exit(
                f"error: [extensions] path must be a non-empty string without "
                f"surrounding whitespace, got {raw_path!r}"
            )

        # Validate shortcut shape before filtering by platform so typos on
        # another OS's entry still fail early when portable syntax is used.
        target_for_shortcut = (
            resolved_platform
            if (entry_platforms is None or resolved_platform in entry_platforms)
            else entry_platforms[0]
        )
        norm_shortcuts: dict[str, str] = {}
        for cmd_name, raw_binding in raw_shortcuts.items():
            if not isinstance(cmd_name, str) or not cmd_name.strip():
                sys.exit(
                    f"error: [extensions] shortcut command name must be a "
                    f"non-empty string, got {cmd_name!r}"
                )
            norm_shortcuts[cmd_name] = normalize_extension_shortcut(
                raw_binding, platform=target_for_shortcut
            )

        if (
            resolved_platform is not None
            and entry_platforms is not None
            and resolved_platform not in entry_platforms
        ):
            continue

        resolved = _resolve_extension_path(raw_path, config_dir)
        _validate_manifest_dir(resolved, raw_path)
        ext_id = unpacked_extension_id(resolved)
        if ext_id in seen_ids:
            continue
        seen_ids.add(ext_id)
        out.append(
            UnpackedEntry(
                path=resolved,
                display_path=raw_path,
                ext_id=ext_id,
                source_sha256=hash_extension_dir(resolved),
                shortcuts=norm_shortcuts,
                incognito=incognito,
            )
        )
    return out


def state_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(prefs_path.name + ".dotbrave.extensions.json")


def _read_sidecar(prefs_path: Path) -> tuple[dict[str, dict[str, Any]], float | None]:
    sf = state_file(prefs_path)
    if not sf.exists():
        return {}, None
    try:
        mtime = sf.stat().st_mtime
        data = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    if not isinstance(data, dict):
        return {}, mtime
    managed = data.get("managed", {})
    if not isinstance(managed, dict):
        return {}, mtime
    return {
        k: v for k, v in managed.items()
        if isinstance(k, str) and isinstance(v, dict)
    }, mtime


def _load_secure_prefs(prefs_path: Path) -> tuple[dict, float]:
    sp_path = prefs_path.with_name("Secure Preferences")
    if not sp_path.exists():
        return {}, 0.0
    try:
        mtime = sp_path.stat().st_mtime
        data = retry_on_permission_error(
            lambda: json.loads(sp_path.read_text(encoding="utf-8")),
            sp_path,
        )
    except (OSError, json.JSONDecodeError):
        return {}, 0.0
    return (data if isinstance(data, dict) else {}), mtime


def _disk_extension_settings(
    prefs_path: Path, prefs: dict,
) -> tuple[dict[str, dict[str, Any]], float]:
    sp, sp_mtime = _load_secure_prefs(prefs_path)
    try:
        pr_mtime = prefs_path.stat().st_mtime
    except OSError:
        pr_mtime = 0.0

    merged: dict[str, dict[str, Any]] = {}
    for src in (prefs, sp):
        ext_block = src.get("extensions", {})
        if not isinstance(ext_block, dict):
            continue
        settings = ext_block.get("settings", {})
        if not isinstance(settings, dict):
            continue
        for ext_id, rec in settings.items():
            if isinstance(ext_id, str) and isinstance(rec, dict):
                merged[ext_id] = {**merged.get(ext_id, {}), **rec}
    return merged, max(pr_mtime, sp_mtime)


def _disk_extension_commands(
    prefs: dict, *, platform: str | None = None,
) -> dict[tuple[str, str], str]:
    prefix = _command_platform_key(platform) + ":"
    cmds = prefs.get("extensions", {}).get("commands", {})
    if not isinstance(cmds, dict):
        return {}
    out: dict[tuple[str, str], str] = {}
    for full_key, entry in cmds.items():
        if (
            not isinstance(full_key, str)
            or not full_key.startswith(prefix)
            or not isinstance(entry, dict)
        ):
            continue
        ext_id = entry.get("extension")
        cmd_name = entry.get("command_name")
        if isinstance(ext_id, str) and isinstance(cmd_name, str):
            out[(ext_id, cmd_name)] = full_key[len(prefix):]
    return out


def _is_healthy_unpacked_record(
    rec: dict[str, Any] | None, expected_path: Path, profile_dir: Path,
) -> bool:
    if not isinstance(rec, dict):
        return False
    if rec.get("location") != _UNPACKED_LOCATION:
        return False
    if rec.get("state") == 0 or bool(rec.get("disable_reasons")):
        return False
    flags = rec.get("creation_flags", 0)
    if isinstance(flags, int) and (flags & _INSTALLED_VIA_CDP_FLAG):
        return False
    raw_rec_path = rec.get("path")
    if not isinstance(raw_rec_path, str) or not raw_rec_path:
        return False
    rec_path = Path(raw_rec_path)
    if not rec_path.is_absolute():
        rec_path = profile_dir / "Extensions" / rec_path
    try:
        a = os.path.normcase(str(rec_path.resolve()))
        b = os.path.normcase(str(expected_path.resolve()))
    except OSError:
        return False
    return a == b


def _mutate_commands_in_prefs(
    prefs: dict,
    writes: list[tuple[str, str, str]],
    uninstall_ids: list[str],
    *,
    platform: str | None = None,
) -> None:
    if not writes and not uninstall_ids:
        return
    prefix = _command_platform_key(platform) + ":"
    cmds = get_nested(prefs, ("extensions", "commands"))
    uninstall_set = set(uninstall_ids)
    target_pairs = {(ext_id, cmd_name) for ext_id, cmd_name, _ in writes}

    for key in list(cmds):
        entry = cmds.get(key)
        if not isinstance(entry, dict):
            continue
        ext_id = entry.get("extension")
        cmd_name = entry.get("command_name")
        if ext_id in uninstall_set:
            cmds.pop(key, None)
        elif key.startswith(prefix) and (ext_id, cmd_name) in target_pairs:
            cmds.pop(key, None)

    for ext_id, cmd_name, binding in writes:
        if binding:
            cmds[f"{prefix}{binding}"] = {
                "command_name": cmd_name,
                "extension": ext_id,
                "global": False,
            }


def plan_apply(
    prefs_path: Path,
    prefs: dict,
    raw_table: object,
    *,
    config_dir: Path | None = None,
    headless_runner: Callable[[Path, str, Plan], None] | None = None,
) -> Plan:
    entries = validate_table(raw_table, config_dir=config_dir)
    profile_dir = prefs_path.parent
    profile_root = profile_dir.parent
    profile_name = profile_dir.name

    disk_settings, disk_mtime = _disk_extension_settings(prefs_path, prefs)
    disk_commands = _disk_extension_commands(prefs)
    managed_before, sidecar_mtime = _read_sidecar(prefs_path)
    # Brave commits Preferences and Secure Preferences on a ~10s delayed timer.
    # While the sidecar is at least as new as both files on disk, trust what a
    # live apply just wrote into Brave's memory rather than re-installing.
    in_commit_window = (
        sidecar_mtime is not None and sidecar_mtime >= disk_mtime
    )

    target_by_id = {e.ext_id: e for e in entries}
    uninstall_ids = sorted(set(managed_before) - set(target_by_id))

    diff: list[str] = []
    entry_ops: list[ExtensionEntryOp] = []
    all_shortcut_writes: list[tuple[str, str, str]] = []

    for entry in entries:
        ext_id = entry.ext_id
        prev = managed_before.get(ext_id)
        rec = disk_settings.get(ext_id)
        installed_on_disk = _is_healthy_unpacked_record(rec, entry.path, profile_dir)
        installed_live = prev is not None and in_commit_window
        is_installed = installed_on_disk or installed_live

        needs_install = not is_installed
        needs_reload = (
            is_installed
            and prev is not None
            and prev.get("source_sha256") != entry.source_sha256
        )
        # First time dotbrave takes ownership of an already-installed unpacked
        # extension: reload once so manifest.json / background.js on disk and
        # the sidecar's source_sha256 are guaranteed in sync.
        if is_installed and prev is None:
            needs_reload = True

        disk_incog = bool(rec.get("incognito")) if isinstance(rec, dict) else False
        prev_incog = prev.get("incognito") if prev is not None else None
        cur_incog = prev_incog if (installed_live and prev_incog is not None) else disk_incog
        needs_incognito = (
            entry.incognito is not None
            and (needs_install or cur_incog != entry.incognito)
        )

        if needs_install:
            diff.append(f"  + {entry.display_path} ({ext_id})")
        elif needs_reload:
            diff.append(f"  ~ {entry.display_path} (reload unpacked extension)")
        if needs_incognito and not needs_install:
            diff.append(
                f"  ~ {entry.display_path} incognito: {cur_incog} -> {entry.incognito}"
            )

        prev_shortcuts = (
            prev.get("shortcuts", {})
            if (prev is not None and isinstance(prev.get("shortcuts"), dict))
            else {}
        )
        shortcut_writes: dict[str, str] = {}
        for cmd_name, want_binding in sorted(entry.shortcuts.items()):
            if installed_live and cmd_name in prev_shortcuts:
                have_binding = prev_shortcuts.get(cmd_name, "")
            else:
                have_binding = disk_commands.get((ext_id, cmd_name), "")
            if needs_install or have_binding != want_binding:
                shortcut_writes[cmd_name] = want_binding
                all_shortcut_writes.append((ext_id, cmd_name, want_binding))
                if not have_binding and want_binding:
                    diff.append(
                        f"  + {entry.display_path} shortcut {cmd_name}: {want_binding}"
                    )
                elif have_binding != want_binding:
                    diff.append(
                        f"  ~ {entry.display_path} shortcut {cmd_name}: "
                        f"{have_binding or '(none)'} -> {want_binding or '(none)'}"
                    )

        for removed_cmd in sorted(set(prev_shortcuts) - set(entry.shortcuts)):
            have_binding = (
                prev_shortcuts.get(removed_cmd, "")
                if installed_live
                else disk_commands.get((ext_id, removed_cmd), "")
            )
            shortcut_writes[removed_cmd] = ""
            all_shortcut_writes.append((ext_id, removed_cmd, ""))
            diff.append(
                f"  - {entry.display_path} shortcut {removed_cmd}"
                + (f": {have_binding} (clear)" if have_binding else " (clear)")
            )

        entry_ops.append(
            ExtensionEntryOp(
                entry=entry,
                needs_install=needs_install,
                needs_reload=needs_reload,
                needs_incognito_update=needs_incognito,
                shortcut_writes=shortcut_writes,
            )
        )

    for rem_id in uninstall_ids:
        rem_label = managed_before.get(rem_id, {}).get("display_path") or (
            managed_before.get(rem_id, {}).get("path") or rem_id
        )
        diff.append(f"  - {rem_label} (uninstall)")

    ops = ExtensionPlanOps(entry_ops=entry_ops, uninstall_ids=uninstall_ids)
    state_payload = {
        "managed": {
            e.ext_id: {
                "path": str(e.path),
                "display_path": e.display_path,
                "source_sha256": e.source_sha256,
                "shortcuts": dict(e.shortcuts),
                **(
                    {"incognito": e.incognito}
                    if e.incognito is not None
                    else {}
                ),
            }
            for e in entries
        }
    }

    def apply_fn(target_prefs: dict) -> None:
        # Shortcut changes live in plain Preferences, so when no install,
        # reload, incognito toggle, or uninstall is needed an offline apply can
        # write them directly without launching headless Brave.
        if not ops.needs_browser_cdp:
            _mutate_commands_in_prefs(
                target_prefs, all_shortcut_writes, uninstall_ids
            )

    def verify_fn(reloaded: dict) -> None:
        if not ops.needs_browser_cdp and all_shortcut_writes:
            actual_cmds = _disk_extension_commands(reloaded)
            for ext_id, cmd_name, want_binding in all_shortcut_writes:
                if actual_cmds.get((ext_id, cmd_name), "") != want_binding:
                    sys.exit(
                        f"error: extensions shortcut verification failed for "
                        f"{ext_id}.{cmd_name}"
                    )

    runner = apply_headless if headless_runner is None else headless_runner
    plan: Plan

    def offline_browser_apply() -> None:
        runner(profile_root, profile_name, plan)

    plan = Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        state_path=state_file(prefs_path),
        state_payload=state_payload,
        offline_browser_apply_fn=(
            offline_browser_apply if ops.needs_browser_cdp else None
        ),
        live_data=ops,
        needs_escalation=False,
    )
    return plan


def _await_extensions_page(client: CdpClient, target: dict, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = client.evaluate(
            target,
            "document.readyState === 'complete' && "
            "typeof chrome !== 'undefined' && "
            "typeof chrome.developerPrivate?.getExtensionsInfo === 'function'",
        )
        if ready is True:
            return
        time.sleep(0.05)
    raise CdpError("chrome://extensions did not expose chrome.developerPrivate")


def preflight_live(client: CdpClient, target: dict, plan: Plan) -> list[str]:
    """Return unsupported reasons when ``chrome://extensions`` or the Browser
    WebSocket needed for ``Extensions.loadUnpacked`` is unavailable."""
    ops: ExtensionPlanOps | None = plan.live_data
    if ops is None or plan.empty:
        return []
    client.navigate(target, _EXTENSIONS_URL)
    try:
        _await_extensions_page(client, target)
    except CdpError:
        return ["extensions (chrome.developerPrivate unavailable)"]
    if ops.needs_browser_cdp:
        ensure_ws = getattr(client, "_ensure_browser_ws", None)
        if ensure_ws is not None:
            try:
                ensure_ws()
            except CdpError:
                return ["extensions (Browser DevTools WebSocket unavailable)"]
    return []


def apply_via_cdp(client: CdpClient, target: dict, plan: Plan) -> None:
    """Apply an ``[extensions]`` plan through a ``chrome://extensions`` work tab
    and Browser-target CDP commands."""
    ops: ExtensionPlanOps | None = plan.live_data
    if ops is None or plan.empty:
        return

    client.navigate(target, _EXTENSIONS_URL)
    _await_extensions_page(client, target)

    for rem_id in ops.uninstall_ids:
        try:
            client.browser_command("Extensions.uninstall", {"id": rem_id})
        except CdpError as e:
            if "could not find extension" not in str(e).lower():
                raise

    if ops.entry_ops and any(
        op.needs_install or op.needs_reload for op in ops.entry_ops
    ):
        client.evaluate(
            target,
            "(async () => {"
            "await chrome.developerPrivate.updateProfileConfiguration("
            "{inDeveloperMode: true});"
            "return true;"
            "})()",
        )

    for op in ops.entry_ops:
        ext_id = op.entry.ext_id
        if op.needs_install:
            params: dict[str, Any] = {"path": str(op.entry.path)}
            if op.entry.incognito is not None:
                params["enableInIncognito"] = op.entry.incognito
            res = client.browser_command("Extensions.loadUnpacked", params)
            loaded_id = res.get("result", {}).get("id")
            if isinstance(loaded_id, str) and loaded_id:
                ext_id = loaded_id

            present = client.evaluate(
                target,
                f"(async () => {{"
                f"const exts = await chrome.developerPrivate.getExtensionsInfo();"
                f"return exts.some(e => e.id === {json.dumps(ext_id)});"
                f"}})()",
            )
            if present is not True:
                try:
                    client.browser_command("Extensions.uninstall", {"id": ext_id})
                except CdpError:
                    pass
                raise CdpError(
                    f"Extensions.loadUnpacked loaded {op.entry.display_path} "
                    f"into Brave's default context rather than this profile"
                )

            # Clear Extension::INSTALLED_VIA_CDP in Secure Preferences so
            # ExtensionPrefs::CleanUpCdpInstalledExtensions() keeps it across
            # browser restarts.
            client.evaluate(
                target,
                f"(async () => {{"
                f"await chrome.developerPrivate.reload("
                f"{json.dumps(ext_id)}, {{failQuietly: true}});"
                f"return true;"
                f"}})()",
            )
        elif op.needs_reload:
            client.evaluate(
                target,
                f"(async () => {{"
                f"await chrome.developerPrivate.reload("
                f"{json.dumps(ext_id)}, {{failQuietly: true}});"
                f"return true;"
                f"}})()",
            )

        if op.needs_incognito_update and op.entry.incognito is not None:
            client.evaluate(
                target,
                f"(async () => {{"
                f"await chrome.developerPrivate.updateExtensionConfiguration({{"
                f"extensionId: {json.dumps(ext_id)}, "
                f"incognitoAccess: {json.dumps(op.entry.incognito)}"
                f"}});"
                f"return true;"
                f"}})()",
            )

        for cmd_name, binding in op.shortcut_writes.items():
            client.evaluate(
                target,
                f"(async () => {{"
                f"await chrome.developerPrivate.updateExtensionCommand({{"
                f"extensionId: {json.dumps(ext_id)}, "
                f"commandName: {json.dumps(cmd_name)}, "
                f"keybinding: {json.dumps(binding)}"
                f"}});"
                f"return true;"
                f"}})()",
            )


def _check_headless_supported(prefs_path: Path) -> None:
    for substr, install_name, _policy_dir in pwa_mod._PWA_CONFIG.sandbox_checks:
        if substr in str(prefs_path):
            sys.exit(
                f"error: offline [extensions] apply is not supported on "
                f"{install_name}. Open Brave with a DevTools endpoint or "
                f"install the official Brave package."
            )


def apply_headless(
    profile_root: Path,
    profile: str,
    plan: Plan,
    *,
    proc=None,
) -> None:
    """Run ``apply_via_cdp`` against a closed profile using a short-lived
    ``--headless=new`` Brave instance so Chromium writes ``Secure Preferences``
    with its own MAC, then shut it down cleanly via ``Browser.close``."""
    from dotbrave.utils import BROWSER_PROCESS

    browser_proc = BROWSER_PROCESS if proc is None else proc
    prefs_path = profile_root / profile / "Preferences"
    _check_headless_supported(prefs_path)

    port = pick_unused_port()
    try:
        cmdline = browser_proc.headless_launch_cmdline(profile_root, profile, port)
    except FileNotFoundError as e:
        sys.exit(
            f"error: cannot apply [extensions] while {browser_proc.display_name} "
            f"is closed -- browser executable not found ({e})"
        )

    child = subprocess.Popen(
        cmdline,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client: CdpClient | None = None
    target: dict | None = None
    try:
        wait_for_devtools_endpoint(port, f"{browser_proc.display_name} (headless)")
        ws_path = discover_browser_ws_path(port)
        client = CdpClient(port, ws_path=ws_path)
        if ws_path:
            client._ensure_browser_ws()
        target = client.create_page(_EXTENSIONS_URL)
        apply_via_cdp(client, target, plan)
    except CdpError as e:
        sys.exit(f"error: headless [extensions] apply failed: {e}")
    finally:
        if client is not None:
            if target is not None:
                client.close_page(target)
            try:
                client.browser_command("Browser.close")
            except CdpError:
                pass
            client.close()
        try:
            child.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                child.kill()


def build_dump_block(prefs_path: Path, prefs: dict) -> list[str] | None:
    """Emit ``[extensions]`` for ``dotbrave export``, or ``None`` when the
    profile has no unpacked extensions."""
    disk_settings, _ = _disk_extension_settings(prefs_path, prefs)
    disk_commands = _disk_extension_commands(prefs)
    profile_dir = prefs_path.parent

    rows: list[tuple[str, dict[str, str]]] = []
    for ext_id, rec in sorted(disk_settings.items()):
        if not isinstance(rec, dict) or rec.get("location") != _UNPACKED_LOCATION:
            continue
        if rec.get("state") == 0 or bool(rec.get("disable_reasons")):
            continue
        raw_path = rec.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        resolved = Path(raw_path)
        if not resolved.is_absolute():
            resolved = profile_dir / "Extensions" / resolved
        sc: dict[str, str] = {}
        for (cid, cmd_name), binding in sorted(disk_commands.items()):
            if cid == ext_id and binding:
                sc[cmd_name] = to_keyevent_shortcut(binding)
        rows.append((str(resolved), sc))

    if not rows:
        return None

    rows.sort(key=lambda item: item[0])
    lines = ["[extensions]", "unpacked = ["]
    for path_str, sc in rows:
        if sc:
            sc_items = ", ".join(
                f"{cmd} = {json.dumps(val)}" for cmd, val in sorted(sc.items())
            )
            lines.append(
                f"  {{ path = {json.dumps(path_str)}, shortcuts = {{ {sc_items} }} }},"
            )
        else:
            lines.append(f"  {json.dumps(path_str)},")
    lines.append("]")
    return lines
