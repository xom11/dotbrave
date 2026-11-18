"""Manage Chrome Web Store and unpacked Brave extensions -- ``[extensions]``.

Two sub-keys live under ``[extensions]``:
- ``webstore = [...]`` writes Chromium's ``ExtensionSettings`` managed policy
  (``installation_mode``, Chrome Web Store ``update_url``, optional
  ``toolbar_pin``) via elevated privileges -- merged into the shared macOS
  plist alongside ``[pwa]`` and ``[shields]``, written to
  ``/etc/brave/policies/managed/dotbrave-extensions.json`` on Linux, and stored
  as a ``REG_SZ`` JSON object on ``HKLM\\Software\\Policies\\BraveSoftware\\Brave``
  on Windows. Optional ``file_access``, ``incognito``, and ``shortcuts`` fields
  on ``webstore`` entries are applied through ``chrome.developerPrivate`` once
  Brave installs the extension.
- ``unpacked = [...]`` loads local unpacked extension directories via CDP:
  - ``extensions.settings`` and ``extensions.ui.developer_mode`` are tracked in
    ``Secure Preferences`` under ``ENFORCE_ON_LOAD`` on macOS and Windows
    (``chrome_pref_service_factory.cc``), so entries written outside Brave are
    wiped on startup.
  - Enterprise policies ``ExtensionInstallForcelist`` / ``ExtensionSettings``
    are filtered with ``[BLOCKED]`` for off-Web-Store ``update_url``s by
    ``FilterSensitivePolicies`` (``policy_loader_common.cc``) on non-MDM macOS
    and non-AD Windows.
  - Plain Browser-target CDP ``Extensions.loadUnpacked`` works on a standard
    ``--remote-debugging-port`` endpoint without extra flags, but stamps
    ``Extension::INSTALLED_VIA_CDP`` (``1 << 15``) onto ``creation_flags``,
    which ``ExtensionPrefs::CleanUpCdpInstalledExtensions()`` deletes on the
    next browser startup.
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
import re
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
POLICY_KEY = "ExtensionSettings"
CHROME_WEBSTORE_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
_LINUX_POLICY_PATH = "/etc/brave/policies/managed/dotbrave-extensions.json"


def _default_policy_file() -> Path | None:
    if sys.platform.startswith("linux"):
        return Path(_LINUX_POLICY_PATH)
    if sys.platform == "darwin":
        return Path(pwa_mod._PWA_CONFIG.macos_plist_path)
    return None


# Module-level state that tests monkeypatch
POLICY_FILE = _default_policy_file()
_WINDOWS_POLICY_KEY = pwa_mod._WINDOWS_POLICY_KEY
_WEBSTORE_INSTALL_WAIT_S = 20.0
_WEBSTORE_INSTALL_POLL_S = 0.25

_EXT_ID_RE = re.compile(r"^[a-p]{32}$")
_UNPACKED_LOCATION = 4
_ALLOW_FILE_ACCESS_FLAG = 1 << 2
_INSTALLED_VIA_CDP_FLAG = 1 << 15
_EXTENSIONS_URL = "chrome://extensions"
_IGNORED_NAMES = frozenset({".git", "__pycache__", ".DS_Store", "Thumbs.db"})
_MISSING = object()

_PIN_TO_POLICY: dict[str, str] = {
    "force": "force_pinned",
    "default": "default_pinned",
    "unpinned": "default_unpinned",
}
_POLICY_TO_PIN: dict[str, str] = {
    "force_pinned": "force",
    "default_pinned": "default",
    "default_unpinned": "unpinned",
}

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
class WebstoreEntry:
    ext_id: str
    name: str | None = None
    installation_mode: str = "force_installed"
    toolbar_pin: str | None = None
    file_access: bool | None = None
    incognito: bool | None = None
    shortcuts: dict[str, str] = field(default_factory=dict)

    @property
    def display_label(self) -> str:
        return f"{self.name} ({self.ext_id})" if self.name else self.ext_id

    def policy_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "installation_mode": self.installation_mode,
            "update_url": CHROME_WEBSTORE_UPDATE_URL,
        }
        if self.toolbar_pin is not None:
            d["toolbar_pin"] = self.toolbar_pin
        return d


@dataclass(frozen=True)
class UnpackedEntry:
    path: Path
    display_path: str
    ext_id: str
    source_sha256: str
    shortcuts: dict[str, str] = field(default_factory=dict)
    incognito: bool | None = None
    file_access: bool | None = None


@dataclass(frozen=True)
class ValidatedExtensions:
    manage_webstore: bool
    webstore: list[WebstoreEntry]
    manage_unpacked: bool
    unpacked: list[UnpackedEntry]


@dataclass(frozen=True)
class ExtensionEntryOp:
    entry: UnpackedEntry
    needs_install: bool
    needs_reload: bool
    needs_incognito_update: bool
    shortcut_writes: dict[str, str]
    needs_file_access_update: bool = False


@dataclass(frozen=True)
class WebstoreEntryOp:
    entry: WebstoreEntry
    needs_incognito_update: bool
    needs_file_access_update: bool
    shortcut_writes: dict[str, str]
    fallback_prefs: dict[str, Any] | None = None
    wait_for_modifiable: bool = False

    @property
    def needs_cdp_action(self) -> bool:
        return bool(
            self.needs_incognito_update
            or self.needs_file_access_update
            or self.shortcut_writes
        )


@dataclass(frozen=True)
class ExtensionPlanOps:
    entry_ops: list[ExtensionEntryOp]
    uninstall_ids: list[str]
    webstore_ops: list[WebstoreEntryOp] = field(default_factory=list)
    removed_webstore_shortcuts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def needs_browser_ws(self) -> bool:
        if self.uninstall_ids:
            return True
        return any(op.needs_install for op in self.entry_ops)

    @property
    def needs_browser_cdp(self) -> bool:
        if self.needs_browser_ws:
            return True
        if any(
            op.needs_reload
            or op.needs_incognito_update
            or op.needs_file_access_update
            for op in self.entry_ops
        ):
            return True
        return any(
            wop.needs_incognito_update or wop.needs_file_access_update
            for wop in self.webstore_ops
        )


def _read_current_policy() -> dict[str, dict[str, Any]]:
    if sys.platform == "win32":
        raw = _base_pwa.read_windows_registry_dict(_WINDOWS_POLICY_KEY, POLICY_KEY)
    else:
        data = _base_pwa.read_existing_payload(POLICY_FILE, _WINDOWS_POLICY_KEY)
        raw = data.get(POLICY_KEY, {})
    if not isinstance(raw, dict):
        return {}
    return {
        ext_id: dict(cfg)
        for ext_id, cfg in raw.items()
        if isinstance(ext_id, str) and isinstance(cfg, dict)
    }


def _sudo_write_policy(mapping: dict[str, dict[str, Any]]) -> None:
    sorted_mapping = {k: mapping[k] for k in sorted(mapping)}
    _base_pwa.sudo_write_policy(
        POLICY_FILE,
        _WINDOWS_POLICY_KEY,
        sorted_mapping,
        policy_key=POLICY_KEY,
        managed_keys=pwa_mod.MANAGED_POLICY_KEYS,
    )


def _check_policy_supported(prefs_path: Path) -> None:
    if sys.platform != "win32" and POLICY_FILE is None:
        sys.exit(
            f"error: [extensions] webstore is not implemented on "
            f"platform={sys.platform!r}. Linux, macOS and Windows are supported."
        )
    for substr, install_name, policy_dir in pwa_mod._PWA_CONFIG.sandbox_checks:
        if substr in str(prefs_path):
            sys.exit(
                f"error: [extensions] webstore is not supported on {install_name} "
                f"(the sandbox does not read {policy_dir}). Install Brave from "
                f"the official package, or remove `webstore` from [extensions]."
            )


def policy_diff_summary(
    current: dict[str, dict[str, Any]],
    entries: list[WebstoreEntry],
) -> list[str]:
    entry_by_id = {e.ext_id: e for e in entries}
    target_policy = {e.ext_id: e.policy_dict() for e in entries}
    target_set = set(target_policy)
    current_set = set(current)
    lines: list[str] = []

    for ext_id in sorted(target_set - current_set):
        e = entry_by_id[ext_id]
        detail = (
            e.installation_mode
            if e.toolbar_pin is None
            else f"{e.installation_mode}, {e.toolbar_pin}"
        )
        lines.append(f"  + {e.display_label} ({detail})")

    for ext_id in sorted(current_set - target_set):
        lines.append(f"  - {ext_id} (uninstall)")

    for ext_id in sorted(target_set & current_set):
        have = current[ext_id]
        want = target_policy[ext_id]
        if have != want:
            e = entry_by_id[ext_id]
            changes: list[str] = []
            for k in sorted(set(have) | set(want)):
                hv = have.get(k, _MISSING)
                wv = want.get(k, _MISSING)
                if hv != wv:
                    h_text = "(none)" if hv is _MISSING else str(hv)
                    w_text = "(none)" if wv is _MISSING else str(wv)
                    changes.append(f"{k}: {h_text} -> {w_text}")
            lines.append(f"  ~ {e.display_label} ({', '.join(changes)})")
    return lines


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


def _validate_platforms_field(plats: object) -> list[str]:
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
    return plats


def _validate_shortcuts_field(
    raw_shortcuts: dict[str, Any],
    *,
    resolved_platform: str | None,
    entry_platforms: list[str] | None,
) -> dict[str, str]:
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
    return norm_shortcuts


def _validate_webstore_id(raw_id: object) -> str:
    if not isinstance(raw_id, str):
        sys.exit('error: [extensions] webstore entry needs a string `id = "..."`')
    if raw_id != raw_id.strip():
        sys.exit(
            f"error: [extensions] webstore id must not have surrounding "
            f"whitespace, got {raw_id!r}"
        )
    if not _EXT_ID_RE.match(raw_id):
        sys.exit(
            f"error: [extensions] invalid Chrome Web Store extension id "
            f"{raw_id!r} (expected 32 lowercase characters in 'a'..'p')"
        )
    return raw_id


def _validate_pin(raw_pin: object) -> str:
    if isinstance(raw_pin, bool):
        return "default_pinned" if raw_pin else "default_unpinned"
    if isinstance(raw_pin, str) and raw_pin in _PIN_TO_POLICY:
        return _PIN_TO_POLICY[raw_pin]
    sys.exit(
        f"error: [extensions] pin must be 'force', 'default', 'unpinned', "
        f"or a boolean, got {raw_pin!r}"
    )


def validate_extensions_table(
    raw: object,
    *,
    config_dir: Path | None = None,
    platform: str | None = _base_pwa._AUTO_PLATFORM,
) -> ValidatedExtensions:
    if not isinstance(raw, dict):
        sys.exit("error: [extensions] must be a table")
    extra = set(raw) - {"webstore", "unpacked"}
    if extra:
        sys.exit(
            f"error: [extensions] has unsupported keys: {sorted(extra)}. "
            f"Only `webstore = [...]` and `unpacked = [...]` are supported"
        )

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

    is_empty_table = len(raw) == 0
    manage_webstore = "webstore" in raw or is_empty_table
    manage_unpacked = "unpacked" in raw or is_empty_table

    webstore_out: list[WebstoreEntry] = []
    if "webstore" in raw:
        ws_items = raw["webstore"]
        if not isinstance(ws_items, list):
            sys.exit("error: [extensions] webstore must be an array")
        seen_ws: set[str] = set()
        for item in ws_items:
            name: str | None = None
            installation_mode = "force_installed"
            toolbar_pin: str | None = None
            file_access: bool | None = None
            incognito: bool | None = None
            raw_shortcuts: dict[str, Any] = {}
            entry_platforms: list[str] | None = None

            if isinstance(item, str):
                ext_id = _validate_webstore_id(item)
            elif isinstance(item, dict):
                bad = set(item) - {
                    "id",
                    "name",
                    "mode",
                    "pin",
                    "file_access",
                    "incognito",
                    "shortcuts",
                    "platforms",
                }
                if bad:
                    sys.exit(
                        f"error: [extensions] webstore entry has unsupported keys: "
                        f"{sorted(bad)} (expected {{ id = \"...\", name = \"...\", "
                        f'mode = "force"|"normal", pin = "force"|"default"|"unpinned"|bool, '
                        f"file_access = bool, incognito = bool, shortcuts = {{...}}, "
                        f"platforms = [...] }})"
                    )
                ext_id = _validate_webstore_id(item.get("id"))
                if "name" in item:
                    raw_name = item["name"]
                    if (
                        not isinstance(raw_name, str)
                        or not raw_name.strip()
                        or raw_name != raw_name.strip()
                    ):
                        sys.exit(
                            f"error: [extensions] name must be a non-empty string "
                            f"without surrounding whitespace, got {raw_name!r}"
                        )
                    name = raw_name
                if "mode" in item:
                    raw_mode = item["mode"]
                    if raw_mode == "force":
                        installation_mode = "force_installed"
                    elif raw_mode == "normal":
                        installation_mode = "normal_installed"
                    else:
                        sys.exit(
                            f"error: [extensions] mode must be 'force' or 'normal', "
                            f"got {raw_mode!r}"
                        )
                if "pin" in item:
                    toolbar_pin = _validate_pin(item["pin"])
                if "file_access" in item:
                    if not isinstance(item["file_access"], bool):
                        sys.exit(
                            f"error: [extensions] file_access must be a boolean, "
                            f"got {item['file_access']!r}"
                        )
                    file_access = item["file_access"]
                if "incognito" in item:
                    if not isinstance(item["incognito"], bool):
                        sys.exit(
                            f"error: [extensions] incognito must be a boolean, "
                            f"got {item['incognito']!r}"
                        )
                    incognito = item["incognito"]
                if "platforms" in item:
                    entry_platforms = _validate_platforms_field(item["platforms"])
                if "shortcuts" in item:
                    sc = item["shortcuts"]
                    if not isinstance(sc, dict):
                        sys.exit("error: [extensions] shortcuts must be a table")
                    raw_shortcuts = sc
            else:
                sys.exit(
                    f"error: [extensions] webstore entries must be strings or tables, "
                    f"got {type(item).__name__}"
                )

            norm_shortcuts = _validate_shortcuts_field(
                raw_shortcuts,
                resolved_platform=resolved_platform,
                entry_platforms=entry_platforms,
            )

            if (
                resolved_platform is not None
                and entry_platforms is not None
                and resolved_platform not in entry_platforms
            ):
                continue
            if ext_id in seen_ws:
                continue
            seen_ws.add(ext_id)
            webstore_out.append(
                WebstoreEntry(
                    ext_id=ext_id,
                    name=name,
                    installation_mode=installation_mode,
                    toolbar_pin=toolbar_pin,
                    file_access=file_access,
                    incognito=incognito,
                    shortcuts=norm_shortcuts,
                )
            )

    unpacked_out: list[UnpackedEntry] = []
    if "unpacked" in raw:
        items = raw["unpacked"]
        if not isinstance(items, list):
            sys.exit("error: [extensions] unpacked must be an array")
        seen_ids: set[str] = set()
        for item in items:
            raw_shortcuts = {}
            incognito = None
            file_access = None
            entry_platforms = None

            if isinstance(item, str):
                raw_path = item
            elif isinstance(item, dict):
                bad = set(item) - {
                    "path",
                    "shortcuts",
                    "incognito",
                    "file_access",
                    "platforms",
                }
                if bad:
                    sys.exit(
                        f"error: [extensions] entry has unsupported keys: {sorted(bad)} "
                        f'(expected {{ path = "...", shortcuts = {{...}}, '
                        f"incognito = bool, file_access = bool, platforms = [...] }})"
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
                if "file_access" in item:
                    if not isinstance(item["file_access"], bool):
                        sys.exit(
                            f"error: [extensions] file_access must be a boolean, "
                            f"got {item['file_access']!r}"
                        )
                    file_access = item["file_access"]
                if "platforms" in item:
                    entry_platforms = _validate_platforms_field(item["platforms"])
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

            norm_shortcuts = _validate_shortcuts_field(
                raw_shortcuts,
                resolved_platform=resolved_platform,
                entry_platforms=entry_platforms,
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
            unpacked_out.append(
                UnpackedEntry(
                    path=resolved,
                    display_path=raw_path,
                    ext_id=ext_id,
                    source_sha256=hash_extension_dir(resolved),
                    shortcuts=norm_shortcuts,
                    incognito=incognito,
                    file_access=file_access,
                )
            )

    return ValidatedExtensions(
        manage_webstore=manage_webstore,
        webstore=webstore_out,
        manage_unpacked=manage_unpacked,
        unpacked=unpacked_out,
    )


def validate_table(
    raw: object,
    *,
    config_dir: Path | None = None,
    platform: str | None = _base_pwa._AUTO_PLATFORM,
) -> list[UnpackedEntry]:
    return validate_extensions_table(
        raw, config_dir=config_dir, platform=platform
    ).unpacked


def state_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(prefs_path.name + ".dotbrave.extensions.json")


def _read_sidecar(
    prefs_path: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    set[str],
    dict[str, dict[str, Any]],
    float | None,
]:
    sf = state_file(prefs_path)
    if not sf.exists():
        return {}, set(), {}, None
    try:
        mtime = sf.stat().st_mtime
        data = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, set(), {}, None
    if not isinstance(data, dict):
        return {}, set(), {}, mtime
    managed = data.get("managed", {})
    raw_uninstalled = data.get("uninstalled_ids", [])
    raw_ws_prefs = data.get("webstore_prefs", {})
    uninstalled = (
        {x for x in raw_uninstalled if isinstance(x, str)}
        if isinstance(raw_uninstalled, list)
        else set()
    )
    managed_clean = (
        {
            k: v
            for k, v in managed.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
        if isinstance(managed, dict)
        else {}
    )
    ws_prefs_clean = (
        {
            k: v
            for k, v in raw_ws_prefs.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
        if isinstance(raw_ws_prefs, dict)
        else {}
    )
    return managed_clean, uninstalled, ws_prefs_clean, mtime


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


def _record_file_access(rec: dict[str, Any] | None) -> bool:
    if not isinstance(rec, dict):
        return False
    if "newAllowFileAccess" in rec:
        return bool(rec["newAllowFileAccess"])
    flags = rec.get("creation_flags", 0)
    return bool(flags & _ALLOW_FILE_ACCESS_FLAG) if isinstance(flags, int) else False


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


def _build_webstore_policy_plan(
    prefs_path: Path,
    entries: list[WebstoreEntry],
    *,
    current_policy: dict[str, dict[str, Any]] | None = None,
) -> Plan:
    _check_policy_supported(prefs_path)
    target_policy = {
        e.ext_id: e.policy_dict()
        for e in sorted(entries, key=lambda item: item.ext_id)
    }
    if current_policy is None:
        current_policy = _read_current_policy()
    diff = policy_diff_summary(current_policy, entries)
    dirty = bool(diff)

    def apply_fn(_prefs: dict) -> None:
        pass

    def verify_fn(_reloaded: dict) -> None:
        pass

    def external_apply_fn() -> None:
        if not dirty:
            return
        _sudo_write_policy(target_policy)
        actual = _read_current_policy()
        if actual != target_policy:
            sys.exit(
                "error: extensions policy verification failed: policy does "
                f"not match config (wrote {sorted(target_policy)}, policy "
                f"has {sorted(actual)})"
            )

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        external_apply_fn=external_apply_fn,
        needs_escalation=dirty,
    )


def _webstore_prefs_record(entry: WebstoreEntry) -> dict[str, Any]:
    rec: dict[str, Any] = {}
    if entry.shortcuts:
        rec["shortcuts"] = dict(entry.shortcuts)
    if entry.incognito is not None:
        rec["incognito"] = entry.incognito
    if entry.file_access is not None:
        rec["file_access"] = entry.file_access
    return rec


def _build_browser_plan(
    prefs_path: Path,
    prefs: dict,
    validated: ValidatedExtensions,
    *,
    current_policy: dict[str, dict[str, Any]] | None = None,
    headless_runner: Callable[[Path, str, Plan], None] | None = None,
) -> Plan | None:
    profile_dir = prefs_path.parent
    profile_root = profile_dir.parent
    profile_name = profile_dir.name

    disk_settings, disk_mtime = _disk_extension_settings(prefs_path, prefs)
    disk_commands = _disk_extension_commands(prefs)
    (
        managed_before,
        uninstalled_before,
        webstore_prefs_before,
        sidecar_mtime,
    ) = _read_sidecar(prefs_path)

    target_webstore_prefs: dict[str, dict[str, Any]] = {}
    if validated.manage_webstore:
        for ws_entry in validated.webstore:
            rec_wp = _webstore_prefs_record(ws_entry)
            if rec_wp:
                target_webstore_prefs[ws_entry.ext_id] = rec_wp
    else:
        target_webstore_prefs = dict(webstore_prefs_before)

    if (
        not validated.manage_unpacked
        and not target_webstore_prefs
        and not webstore_prefs_before
    ):
        return None

    # Brave commits Preferences and Secure Preferences on a ~10s delayed timer.
    # While the sidecar is at least as new as both files on disk, trust what a
    # live apply just wrote into Brave's memory rather than reading stale disk.
    in_commit_window = (
        sidecar_mtime is not None and sidecar_mtime >= disk_mtime
    )

    diff: list[str] = []
    entry_ops: list[ExtensionEntryOp] = []
    webstore_ops: list[WebstoreEntryOp] = []
    removed_webstore_shortcuts: list[tuple[str, str]] = []
    all_shortcut_writes: list[tuple[str, str, str]] = []

    if validated.manage_unpacked:
        entries = validated.unpacked
        target_by_id = {e.ext_id: e for e in entries}
        uninstall_ids = sorted(set(managed_before) - set(target_by_id))

        for entry in entries:
            ext_id = entry.ext_id
            prev = managed_before.get(ext_id)
            rec = disk_settings.get(ext_id)
            just_uninstalled = in_commit_window and ext_id in uninstalled_before
            installed_on_disk = (
                not just_uninstalled
                and _is_healthy_unpacked_record(rec, entry.path, profile_dir)
            )
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
            prev_incog = (
                prev.get("incognito")
                if (prev is not None and isinstance(prev.get("incognito"), bool))
                else None
            )
            cur_incog = (
                prev_incog
                if (installed_live and prev_incog is not None)
                else disk_incog
            )
            needs_incognito = (
                entry.incognito is not None
                and (needs_install or cur_incog != entry.incognito)
            )

            disk_fa = _record_file_access(rec)
            prev_fa = (
                prev.get("file_access")
                if (prev is not None and isinstance(prev.get("file_access"), bool))
                else None
            )
            cur_fa = prev_fa if (installed_live and prev_fa is not None) else disk_fa
            needs_file_access = (
                entry.file_access is not None
                and (needs_install or cur_fa != entry.file_access)
            )

            if needs_install:
                diff.append(f"  + {entry.display_path} ({ext_id})")
            elif needs_reload:
                diff.append(f"  ~ {entry.display_path} (reload unpacked extension)")
            if needs_incognito and not needs_install:
                diff.append(
                    f"  ~ {entry.display_path} incognito: "
                    f"{cur_incog} -> {entry.incognito}"
                )
            if needs_file_access and not needs_install:
                diff.append(
                    f"  ~ {entry.display_path} file_access: "
                    f"{cur_fa} -> {entry.file_access}"
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
                            f"  + {entry.display_path} shortcut {cmd_name}: "
                            f"{want_binding}"
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
                    needs_file_access_update=needs_file_access,
                )
            )

        for rem_id in uninstall_ids:
            rem_label = managed_before.get(rem_id, {}).get("display_path") or (
                managed_before.get(rem_id, {}).get("path") or rem_id
            )
            diff.append(f"  - {rem_label} (uninstall)")

        carried_uninstalled = sorted(
            (set(uninstall_ids) | (uninstalled_before if in_commit_window else set()))
            - set(target_by_id)
        )
        managed_state = {
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
                **(
                    {"file_access": e.file_access}
                    if e.file_access is not None
                    else {}
                ),
            }
            for e in entries
        }
    else:
        uninstall_ids = []
        carried_uninstalled = (
            sorted(uninstalled_before) if in_commit_window and uninstalled_before else []
        )
        managed_state = dict(managed_before)

    if validated.manage_webstore:
        ws_by_id = {e.ext_id: e for e in validated.webstore}
        for ws_entry in validated.webstore:
            ext_id = ws_entry.ext_id
            prev_wp = webstore_prefs_before.get(ext_id)
            rec = disk_settings.get(ext_id)
            live_wp = prev_wp is not None and in_commit_window
            prev_pol = (
                current_policy.get(ext_id)
                if current_policy is not None
                else None
            )
            # Two policy transitions invalidate on-disk Secure Preferences:
            # 1) Adding an ID not currently in ExtensionSettings (`prev_pol is
            #    None`) may land inside Brave's ~10s commit window right after a
            #    policy uninstall left stale `incognito`/`newAllowFileAccess` on
            #    disk.
            # 2) Downgrading `force_installed` -> `normal_installed` causes
            #    Chromium's `ExternalPolicyLoader(FORCED)` to uninstall the
            #    extension and reinstall it fresh with `incognito = false` and
            #    `fileAccess = false`.
            is_new_to_policy = current_policy is not None and prev_pol is None
            is_mode_downgrade = (
                prev_pol is not None
                and prev_pol.get("installation_mode") == "force_installed"
                and ws_entry.installation_mode == "normal_installed"
            )
            policy_reinstall = is_new_to_policy or is_mode_downgrade

            disk_incog = bool(rec.get("incognito")) if isinstance(rec, dict) else False
            prev_incog = (
                prev_wp.get("incognito")
                if (prev_wp is not None and isinstance(prev_wp.get("incognito"), bool))
                else None
            )
            cur_incog = (
                prev_incog if (live_wp and prev_incog is not None) else disk_incog
            )
            needs_incognito = ws_entry.incognito is not None and (
                policy_reinstall or cur_incog != ws_entry.incognito
            )

            disk_fa = _record_file_access(rec)
            prev_fa = (
                prev_wp.get("file_access")
                if (
                    prev_wp is not None
                    and isinstance(prev_wp.get("file_access"), bool)
                )
                else None
            )
            cur_fa = prev_fa if (live_wp and prev_fa is not None) else disk_fa
            needs_file_access = ws_entry.file_access is not None and (
                policy_reinstall or cur_fa != ws_entry.file_access
            )

            if needs_incognito:
                if cur_incog != ws_entry.incognito:
                    diff.append(
                        f"  ~ {ws_entry.display_label} incognito: "
                        f"{cur_incog} -> {ws_entry.incognito}"
                    )
                else:
                    diff.append(
                        f"  ~ {ws_entry.display_label} incognito: "
                        f"{ws_entry.incognito} (reapply)"
                    )
            if needs_file_access:
                if cur_fa != ws_entry.file_access:
                    diff.append(
                        f"  ~ {ws_entry.display_label} file_access: "
                        f"{cur_fa} -> {ws_entry.file_access}"
                    )
                else:
                    diff.append(
                        f"  ~ {ws_entry.display_label} file_access: "
                        f"{ws_entry.file_access} (reapply)"
                    )

            prev_shortcuts = (
                prev_wp.get("shortcuts", {})
                if (prev_wp is not None and isinstance(prev_wp.get("shortcuts"), dict))
                else {}
            )
            ws_shortcut_writes: dict[str, str] = {}
            for cmd_name, want_binding in sorted(ws_entry.shortcuts.items()):
                if live_wp and cmd_name in prev_shortcuts:
                    have_binding = prev_shortcuts.get(cmd_name, "")
                else:
                    have_binding = disk_commands.get((ext_id, cmd_name), "")
                if (policy_reinstall and bool(want_binding)) or (
                    have_binding != want_binding
                ):
                    ws_shortcut_writes[cmd_name] = want_binding
                    all_shortcut_writes.append((ext_id, cmd_name, want_binding))
                    if not have_binding and want_binding:
                        diff.append(
                            f"  + {ws_entry.display_label} shortcut {cmd_name}: "
                            f"{want_binding}"
                        )
                    elif have_binding != want_binding:
                        diff.append(
                            f"  ~ {ws_entry.display_label} shortcut {cmd_name}: "
                            f"{have_binding or '(none)'} -> {want_binding or '(none)'}"
                        )
                    else:
                        diff.append(
                            f"  ~ {ws_entry.display_label} shortcut {cmd_name}: "
                            f"{want_binding} (reapply)"
                        )

            for removed_cmd in sorted(set(prev_shortcuts) - set(ws_entry.shortcuts)):
                have_binding = (
                    prev_shortcuts.get(removed_cmd, "")
                    if live_wp
                    else disk_commands.get((ext_id, removed_cmd), "")
                )
                ws_shortcut_writes[removed_cmd] = ""
                all_shortcut_writes.append((ext_id, removed_cmd, ""))
                diff.append(
                    f"  - {ws_entry.display_label} shortcut {removed_cmd}"
                    + (f": {have_binding} (clear)" if have_binding else " (clear)")
                )

            if needs_incognito or needs_file_access or ws_shortcut_writes:
                webstore_ops.append(
                    WebstoreEntryOp(
                        entry=ws_entry,
                        needs_incognito_update=needs_incognito,
                        needs_file_access_update=needs_file_access,
                        shortcut_writes=ws_shortcut_writes,
                        fallback_prefs=dict(prev_wp) if prev_wp is not None else None,
                        wait_for_modifiable=is_mode_downgrade,
                    )
                )

        for rem_ws_id in sorted(set(webstore_prefs_before) - set(ws_by_id)):
            rem_prev = webstore_prefs_before[rem_ws_id]
            rem_sc = rem_prev.get("shortcuts", {})
            if not isinstance(rem_sc, dict):
                continue
            for removed_cmd in sorted(rem_sc):
                if not isinstance(removed_cmd, str):
                    continue
                have_binding = (
                    rem_sc.get(removed_cmd, "")
                    if in_commit_window
                    else disk_commands.get((rem_ws_id, removed_cmd), "")
                )
                removed_webstore_shortcuts.append((rem_ws_id, removed_cmd))
                all_shortcut_writes.append((rem_ws_id, removed_cmd, ""))
                diff.append(
                    f"  - {rem_ws_id} shortcut {removed_cmd}"
                    + (f": {have_binding} (clear)" if have_binding else " (clear)")
                )

    ops = ExtensionPlanOps(
        entry_ops=entry_ops,
        uninstall_ids=uninstall_ids,
        webstore_ops=webstore_ops,
        removed_webstore_shortcuts=removed_webstore_shortcuts,
    )

    state_payload: dict[str, Any] = {
        "managed": managed_state,
        **(
            {"uninstalled_ids": carried_uninstalled}
            if carried_uninstalled
            else {}
        ),
        **(
            {"webstore_prefs": target_webstore_prefs}
            if target_webstore_prefs
            else {}
        ),
    }

    def apply_fn(target_prefs: dict) -> None:
        # Shortcut changes live in plain Preferences, so when no install,
        # reload, incognito/file_access toggle, or unpacked uninstall is needed
        # an offline apply can write them directly without launching headless
        # Brave. Removed webstore shortcuts are always safe to prune upfront.
        if not ops.needs_browser_cdp:
            _mutate_commands_in_prefs(
                target_prefs, all_shortcut_writes, uninstall_ids
            )
        elif removed_webstore_shortcuts:
            _mutate_commands_in_prefs(
                target_prefs,
                [(eid, cmd, "") for eid, cmd in removed_webstore_shortcuts],
                [],
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


def plan_apply_all(
    prefs_path: Path,
    prefs: dict,
    raw_table: object,
    *,
    config_dir: Path | None = None,
    headless_runner: Callable[[Path, str, Plan], None] | None = None,
) -> list[Plan]:
    validated = validate_extensions_table(raw_table, config_dir=config_dir)
    plans: list[Plan] = []
    current_policy: dict[str, dict[str, Any]] | None = None
    if validated.manage_webstore:
        _check_policy_supported(prefs_path)
        current_policy = _read_current_policy()
        plans.append(
            _build_webstore_policy_plan(
                prefs_path, validated.webstore, current_policy=current_policy
            )
        )
    browser_plan = _build_browser_plan(
        prefs_path,
        prefs,
        validated,
        current_policy=current_policy,
        headless_runner=headless_runner,
    )
    if browser_plan is not None:
        plans.append(browser_plan)
    return plans


def plan_apply(
    prefs_path: Path,
    prefs: dict,
    raw_table: object,
    *,
    config_dir: Path | None = None,
    headless_runner: Callable[[Path, str, Plan], None] | None = None,
) -> Plan:
    plans = plan_apply_all(
        prefs_path,
        prefs,
        raw_table,
        config_dir=config_dir,
        headless_runner=headless_runner,
    )
    if len(plans) == 1:
        return plans[0]
    ext_plan, browser_plan = plans[0], plans[1]
    if ext_plan.empty:
        return browser_plan
    if browser_plan.empty:
        return ext_plan
    return Plan(
        namespace=NAMESPACE,
        diff_lines=ext_plan.diff_lines + browser_plan.diff_lines,
        apply_fn=browser_plan.apply_fn,
        verify_fn=browser_plan.verify_fn,
        state_path=browser_plan.state_path,
        state_payload=browser_plan.state_payload,
        external_apply_fn=ext_plan.external_apply_fn,
        offline_browser_apply_fn=browser_plan.offline_browser_apply_fn,
        live_data=browser_plan.live_data,
        needs_escalation=ext_plan.requires_escalation,
    )


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
    if ops.needs_browser_ws:
        ensure_ws = getattr(client, "_ensure_browser_ws", None)
        if ensure_ws is not None:
            try:
                ensure_ws()
            except CdpError:
                return ["extensions (Browser DevTools WebSocket unavailable)"]
    return []


def _query_live_extension_ids(client: CdpClient, target: dict) -> set[str] | None:
    live_ids_raw = client.evaluate(
        target,
        "(async () => ("
        "await chrome.developerPrivate.getExtensionsInfo()"
        ").map(e => e.id))()",
    )
    if not isinstance(live_ids_raw, list):
        return None
    return {x for x in live_ids_raw if isinstance(x, str)}


def _query_live_webstore_info(
    client: CdpClient, target: dict
) -> dict[str, bool | None] | None:
    raw = client.evaluate(
        target,
        "(async () => ("
        "await chrome.developerPrivate.getExtensionsInfo()"
        ").map(e => ({id: e.id, userMayModify: e.userMayModify})))()",
    )
    if not isinstance(raw, list):
        return None
    out: dict[str, bool | None] = {}
    for item in raw:
        if isinstance(item, str):
            out[item] = None
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            umm = item.get("userMayModify")
            out[item["id"]] = umm if isinstance(umm, bool) else None
    return out


def _is_webstore_op_ready(
    live_info: dict[str, bool | None] | None, wop: WebstoreEntryOp
) -> bool:
    if live_info is None:
        return True
    ext_id = wop.entry.ext_id
    if ext_id not in live_info:
        return False
    # During a `force_installed` -> `normal_installed` downgrade, the outgoing
    # force-installed instance (`userMayModify: false`) stays visible briefly
    # before Chromium uninstalls it and installs the normal copy (`true`).
    if wop.wait_for_modifiable and live_info[ext_id] is False:
        return False
    return True


def _wait_for_webstore_ops(
    client: CdpClient, target: dict, active_ops: list[WebstoreEntryOp]
) -> dict[str, bool | None] | None:
    live_info = _query_live_webstore_info(client, target)
    if (
        live_info is None
        or all(_is_webstore_op_ready(live_info, wop) for wop in active_ops)
        or _WEBSTORE_INSTALL_WAIT_S <= 0
    ):
        return live_info
    deadline = time.monotonic() + _WEBSTORE_INSTALL_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(_WEBSTORE_INSTALL_POLL_S)
        live_info = _query_live_webstore_info(client, target)
        if live_info is None or all(
            _is_webstore_op_ready(live_info, wop) for wop in active_ops
        ):
            return live_info
    return live_info


def _apply_extension_config_and_shortcuts(
    client: CdpClient,
    target: dict,
    ext_id: str,
    *,
    incognito: bool | None,
    file_access: bool | None,
    shortcut_writes: dict[str, str],
) -> None:
    if incognito is not None:
        client.evaluate(
            target,
            f"(async () => {{"
            f"await chrome.developerPrivate.updateExtensionConfiguration({{"
            f"extensionId: {json.dumps(ext_id)}, "
            f"incognitoAccess: {json.dumps(incognito)}"
            f"}});"
            f"return true;"
            f"}})()",
        )
    if file_access is not None:
        client.evaluate(
            target,
            f"(async () => {{"
            f"await chrome.developerPrivate.updateExtensionConfiguration({{"
            f"extensionId: {json.dumps(ext_id)}, "
            f"fileAccess: {json.dumps(file_access)}"
            f"}});"
            f"return true;"
            f"}})()",
        )
    for cmd_name, binding in shortcut_writes.items():
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


def _revert_unapplied_webstore_state(plan: Plan, wop: WebstoreEntryOp) -> None:
    payload = plan.state_payload
    if not isinstance(payload, dict):
        return
    ws_prefs = payload.get("webstore_prefs")
    if not isinstance(ws_prefs, dict):
        return
    if wop.fallback_prefs is not None:
        ws_prefs[wop.entry.ext_id] = dict(wop.fallback_prefs)
    else:
        ws_prefs.pop(wop.entry.ext_id, None)
    if not ws_prefs:
        payload.pop("webstore_prefs", None)


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

    live_ids = _query_live_extension_ids(client, target)

    for op in ops.entry_ops:
        ext_id = op.entry.ext_id
        should_install = op.needs_install or (
            live_ids is not None and ext_id not in live_ids
        )
        if should_install:
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

        _apply_extension_config_and_shortcuts(
            client,
            target,
            ext_id,
            incognito=op.entry.incognito if op.needs_incognito_update else None,
            file_access=op.entry.file_access if op.needs_file_access_update else None,
            shortcut_writes=op.shortcut_writes,
        )

    active_ws_ops = [wop for wop in ops.webstore_ops if wop.needs_cdp_action]
    if active_ws_ops:
        live_info = _wait_for_webstore_ops(client, target, active_ws_ops)
        for wop in active_ws_ops:
            ext_id = wop.entry.ext_id
            if not _is_webstore_op_ready(live_info, wop):
                sys.stdout.flush()
                print(
                    f"warning: [extensions] {wop.entry.display_label} is not "
                    f"installed yet (waiting for Chrome Web Store download); "
                    f"its permissions/shortcuts will apply on the next run",
                    file=sys.stderr,
                )
                _revert_unapplied_webstore_state(plan, wop)
                continue
            _apply_extension_config_and_shortcuts(
                client,
                target,
                ext_id,
                incognito=(
                    wop.entry.incognito if wop.needs_incognito_update else None
                ),
                file_access=(
                    wop.entry.file_access if wop.needs_file_access_update else None
                ),
                shortcut_writes=wop.shortcut_writes,
            )

    if ops.removed_webstore_shortcuts:
        if live_ids is None:
            live_ids = _query_live_extension_ids(client, target)
        for rem_id, cmd_name in ops.removed_webstore_shortcuts:
            if live_ids is not None and rem_id not in live_ids:
                continue
            try:
                client.evaluate(
                    target,
                    f"(async () => {{"
                    f"await chrome.developerPrivate.updateExtensionCommand({{"
                    f"extensionId: {json.dumps(rem_id)}, "
                    f"commandName: {json.dumps(cmd_name)}, "
                    f'keybinding: ""'
                    f"}});"
                    f"return true;"
                    f"}})()",
                )
            except CdpError:
                pass


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


def _format_shortcuts_inline(sc: dict[str, str]) -> str:
    return ", ".join(
        f"{cmd} = {json.dumps(val)}" for cmd, val in sorted(sc.items())
    )


def build_dump_block(prefs_path: Path, prefs: dict) -> list[str] | None:
    """Emit ``[extensions]`` for ``dotbrave export``, or ``None`` when neither
    managed ``ExtensionSettings`` nor unpacked extensions exist."""
    disk_settings, _ = _disk_extension_settings(prefs_path, prefs)
    disk_commands = _disk_extension_commands(prefs)
    profile_dir = prefs_path.parent

    webstore_policy = (
        _read_current_policy()
        if (POLICY_FILE is not None or sys.platform == "win32")
        else {}
    )
    webstore_lines: list[str] = []
    for ext_id, pol in sorted(webstore_policy.items()):
        if not _EXT_ID_RE.match(ext_id):
            continue
        inst_mode = pol.get("installation_mode")
        if inst_mode == "normal_installed":
            mode = "normal"
        elif inst_mode in ("force_installed", None):
            mode = "force"
        else:
            continue
        raw_pin = pol.get("toolbar_pin")
        pin = _POLICY_TO_PIN.get(raw_pin) if isinstance(raw_pin, str) else None

        rec = disk_settings.get(ext_id)
        incog = bool(rec.get("incognito")) if isinstance(rec, dict) else False
        file_acc = (
            bool(rec["newAllowFileAccess"])
            if (isinstance(rec, dict) and "newAllowFileAccess" in rec)
            else False
        )
        sc: dict[str, str] = {}
        for (cid, cmd_name), binding in sorted(disk_commands.items()):
            if cid == ext_id and binding:
                sc[cmd_name] = to_keyevent_shortcut(binding)

        if (
            mode == "force"
            and pin is None
            and not file_acc
            and not incog
            and not sc
        ):
            webstore_lines.append(f"  {json.dumps(ext_id)},")
        else:
            fields = [f"id = {json.dumps(ext_id)}"]
            if mode != "force":
                fields.append(f"mode = {json.dumps(mode)}")
            if pin is not None:
                fields.append(f"pin = {json.dumps(pin)}")
            if file_acc:
                fields.append("file_access = true")
            if incog:
                fields.append("incognito = true")
            if sc:
                fields.append(f"shortcuts = {{ {_format_shortcuts_inline(sc)} }}")
            webstore_lines.append(f"  {{ {', '.join(fields)} }},")

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
        sc = {}
        for (cid, cmd_name), binding in sorted(disk_commands.items()):
            if cid == ext_id and binding:
                sc[cmd_name] = to_keyevent_shortcut(binding)
        rows.append((str(resolved), sc))

    if not webstore_lines and not rows:
        return None

    lines = ["[extensions]"]
    if webstore_lines:
        lines.append("webstore = [")
        lines.extend(webstore_lines)
        lines.append("]")
    if rows:
        rows.sort(key=lambda item: item[0])
        lines.append("unpacked = [")
        for path_str, sc in rows:
            if sc:
                lines.append(
                    f"  {{ path = {json.dumps(path_str)}, "
                    f"shortcuts = {{ {_format_shortcuts_inline(sc)} }} }},"
                )
            else:
                lines.append(f"  {json.dumps(path_str)},")
        lines.append("]")
    return lines
