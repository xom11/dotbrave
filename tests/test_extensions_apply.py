"""Tests for `[extensions]` unpacked extension and shortcut management."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotbrave import browser as brave_pkg
from dotbrave import extensions, live


def _make_ext(root: Path, name: str = "tabjump", *, version: str = "0.1.0") -> Path:
    ext_dir = root / name
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": name,
                "version": version,
                "action": {"default_popup": "popup.html"},
                "commands": {
                    "_execute_action": {
                        "suggested_key": {"default": "Ctrl+B", "mac": "MacCtrl+B"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "popup.js").write_text("console.log('v1');\n", encoding="utf-8")
    return ext_dir


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    root = tmp_path / "profile_root"
    prof = root / "Default"
    prof.mkdir(parents=True)
    (prof / "Preferences").write_text(json.dumps({"brave": {"accelerators": {}}}))
    return root


class _FakeExtCdpClient:
    def __init__(self, profile_dir: Path, installed_ids: set[str] | None = None):
        self.profile_dir = str(profile_dir)
        self.installed_ids: set[str] = set(installed_ids or ())
        self.browser_calls: list[tuple[str, dict | None]] = []
        self.evaluations: list[str] = []
        self.navigations: list[str] = []

    def bind_profile_root(self, _root: Path) -> None:
        pass

    def _ensure_browser_ws(self) -> None:
        pass

    def create_page(self, url: str = "about:blank") -> dict:
        return {"id": "work-1", "type": "page", "url": url}

    def close_page(self, _target: dict) -> None:
        pass

    def close(self) -> None:
        pass

    def navigate(self, target: dict, url: str) -> None:
        self.navigations.append(url)
        target["url"] = url

    def browser_command(self, method: str, params: dict | None = None) -> dict:
        self.browser_calls.append((method, params))
        if method == "Extensions.loadUnpacked":
            assert params is not None
            eid = extensions.unpacked_extension_id(params["path"])
            self.installed_ids.add(eid)
            return {"result": {"id": eid}}
        if method == "Extensions.uninstall":
            assert params is not None
            self.installed_ids.discard(params["id"])
            return {"result": {}}
        return {"result": {}}

    def evaluate(self, _target: dict, expression: str):
        self.evaluations.append(expression)
        if "getElementById('profile_path')" in expression:
            return self.profile_dir
        if "typeof chrome.developerPrivate?.getExtensionsInfo" in expression:
            return True
        if "readyState" in expression:
            return True
        if "exts.some(e => e.id ===" in expression:
            return any(f'"{eid}"' in expression for eid in self.installed_ids)
        return True


# ---------------------------------------------------------------------------
# ID hashing & shortcut normalization
# ---------------------------------------------------------------------------


def test_unpacked_extension_id_posix_and_windows() -> None:
    posix_path = "/Users/lenamkhanh/.nix/home-manager/dotfiles/browser/tabjump"
    assert (
        extensions.unpacked_extension_id(posix_path, platform="darwin")
        == "mbnkfcpbnmmhjnlfdjiheoaeeibhmjag"
    )
    assert (
        extensions.unpacked_extension_id(posix_path, platform="linux")
        == "mbnkfcpbnmmhjnlfdjiheoaeeibhmjag"
    )

    # Windows normalizes drive letter to uppercase and hashes UTF-16LE bytes.
    win_lower = r"c:\Users\kln\.nix\tabjump"
    win_upper = r"C:\Users\kln\.nix\tabjump"
    win_ext = r"\\?\c:\Users\kln\.nix\tabjump"
    expected_digest = hashlib.sha256(win_upper.encode("utf-16-le")).digest()[:16]
    expected_id = "".join(
        chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0x0F))
        for b in expected_digest
    )
    assert extensions.unpacked_extension_id(win_lower, platform="win32") == expected_id
    assert extensions.unpacked_extension_id(win_upper, platform="win32") == expected_id
    assert extensions.unpacked_extension_id(win_ext, platform="win32") == expected_id


@pytest.mark.parametrize(
    "raw, plat, expected",
    [
        ("Control+KeyB", "macos", "Ctrl+B"),
        ("Control+KeyB", "linux", "Ctrl+B"),
        ("Control+KeyB", "windows", "Ctrl+B"),
        ("MacCtrl+B", "macos", "Ctrl+B"),
        ("Alt+Shift+KeyD", "linux", "Alt+Shift+D"),
        ("Meta+Shift+KeyY", "macos", "Command+Shift+Y"),
        ("Command+Digit9", "macos", "Command+9"),
        ("Alt+ArrowUp", "windows", "Alt+Up"),
        ("MediaPlayPause", "linux", "MediaPlayPause"),
        ("", "linux", ""),
    ],
)
def test_normalize_extension_shortcut(raw: str, plat: str, expected: str) -> None:
    assert extensions.normalize_extension_shortcut(raw, platform=plat) == expected


def test_shortcut_roundtrip_to_keyevent() -> None:
    assert extensions.to_keyevent_shortcut("Ctrl+B") == "Control+KeyB"
    assert extensions.to_keyevent_shortcut("Command+Shift+Y") == "Meta+Shift+KeyY"
    assert extensions.to_keyevent_shortcut("Alt+Up") == "Alt+ArrowUp"
    assert extensions.to_keyevent_shortcut("Ctrl+9") == "Control+Digit9"


@pytest.mark.parametrize(
    "raw, plat, needle",
    [
        ("Meta+KeyB", "linux", "only support on macOS"),
        ("Command+KeyB", "windows", "only support on macOS"),
        ("Shift+KeyB", "linux", "requires Control, Alt"),
        ("KeyB", "macos", "requires Control, Alt"),
        (" Ctrl+B", "linux", "surrounding whitespace"),
        ("Ctrl+F13", "linux", "unsupported key"),
    ],
)
def test_normalize_extension_shortcut_rejects_invalid(
    raw: str, plat: str, needle: str
) -> None:
    with pytest.raises(SystemExit) as e:
        extensions.normalize_extension_shortcut(raw, platform=plat)
    assert needle in str(e.value)


# ---------------------------------------------------------------------------
# Validation & relative path resolution
# ---------------------------------------------------------------------------


def test_validate_resolves_relative_to_config_dir(tmp_path: Path) -> None:
    browser_dir = tmp_path / "browser"
    cfg_dir = browser_dir / "dotbrave"
    cfg_dir.mkdir(parents=True)
    ext_dir = _make_ext(browser_dir, "tabjump")

    entries = extensions.validate_table(
        {
            "unpacked": [
                {
                    "path": "../tabjump",
                    "shortcuts": {"_execute_action": "Control+KeyB"},
                }
            ]
        },
        config_dir=cfg_dir,
    )
    assert len(entries) == 1
    assert entries[0].path == ext_dir.resolve()
    assert entries[0].shortcuts == {"_execute_action": "Ctrl+B"}


def test_validate_rejects_relative_path_for_remote_url() -> None:
    with pytest.raises(SystemExit) as e:
        extensions.validate_table({"unpacked": ["../tabjump"]}, config_dir=None)
    assert "local TOML file" in str(e.value)


def test_validate_filters_by_platform(tmp_path: Path) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    entries = extensions.validate_table(
        {
            "unpacked": [
                {"path": str(ext_dir), "platforms": ["linux"]},
                {"path": "/nonexistent/other/os", "platforms": ["macos"]},
            ]
        },
        config_dir=tmp_path,
        platform="linux",
    )
    assert [e.path for e in entries] == [ext_dir.resolve()]


# ---------------------------------------------------------------------------
# Live apply: install + reload (clear INSTALLED_VIA_CDP) + shortcut + reload on edit + uninstall
# ---------------------------------------------------------------------------


def test_live_apply_installs_reloads_and_binds_shortcut(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "dotbrave"
    cfg_dir.mkdir()
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = cfg_dir / "brave.toml"
    cfg.write_text(
        '[extensions]\n'
        'unpacked = [\n'
        '  { path = "../tabjump", shortcuts = { _execute_action = "Control+KeyB" } },\n'
        ']\n',
        encoding="utf-8",
    )

    fake_cdp = _FakeExtCdpClient(profile_root / "Default")
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    from dotbrave._base import orchestrator as orch
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(cfg),
        dry_run=False,
        channel="stable",
    )
    brave_pkg.cmd_apply(args)

    ext_id = extensions.unpacked_extension_id(ext_dir.resolve())
    assert ("Extensions.loadUnpacked", {"path": str(ext_dir.resolve())}) in fake_cdp.browser_calls
    assert any(
        "chrome.developerPrivate.reload" in expr and ext_id in expr
        for expr in fake_cdp.evaluations
    )
    assert any(
        "chrome.developerPrivate.updateExtensionCommand" in expr
        and '"_execute_action"' in expr
        and '"Ctrl+B"' in expr
        for expr in fake_cdp.evaluations
    )

    # Immediate re-apply within Brave's delayed flush window is a no-op.
    fake_cdp.browser_calls.clear()
    fake_cdp.evaluations.clear()
    brave_pkg.cmd_apply(args)
    assert fake_cdp.browser_calls == []
    assert fake_cdp.evaluations == []

    # Editing a file inside tabjump triggers a live reload without re-installing.
    (ext_dir / "popup.js").write_text("console.log('v2');\n", encoding="utf-8")
    brave_pkg.cmd_apply(args)
    assert fake_cdp.browser_calls == []
    assert any(
        "chrome.developerPrivate.reload" in expr and ext_id in expr
        for expr in fake_cdp.evaluations
    )

    # Emptying [extensions] uninstalls the previously managed unpacked extension.
    cfg.write_text("[extensions]\nunpacked = []\n", encoding="utf-8")
    fake_cdp.browser_calls.clear()
    brave_pkg.cmd_apply(args)
    assert ("Extensions.uninstall", {"id": ext_id}) in fake_cdp.browser_calls


# ---------------------------------------------------------------------------
# Offline apply: headless runner for install, direct Preferences write for shortcuts
# ---------------------------------------------------------------------------


def test_offline_apply_uses_headless_for_install_and_direct_prefs_for_shortcut_edit(
    profile_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    ext_id = extensions.unpacked_extension_id(ext_dir.resolve())
    profile_dir = profile_root / "Default"
    prefs_path = profile_dir / "Preferences"
    sp_path = profile_dir / "Secure Preferences"

    headless_runs: list[str] = []

    def fake_apply_headless(root: Path, prof: str, plan) -> None:
        headless_runs.append(prof)
        # Simulate Brave flushing Secure Preferences + Preferences on Browser.close.
        sp_path.write_text(
            json.dumps(
                {
                    "extensions": {
                        "settings": {
                            ext_id: {
                                "location": 4,
                                "path": str(ext_dir.resolve()),
                                "creation_flags": 38,
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        p = json.loads(prefs_path.read_text(encoding="utf-8"))
        plat_key = extensions._command_platform_key()
        p.setdefault("extensions", {}).setdefault("commands", {})[
            f"{plat_key}:Ctrl+B"
        ] = {
            "command_name": "_execute_action",
            "extension": ext_id,
            "global": False,
        }
        prefs_path.write_text(json.dumps(p), encoding="utf-8")

    monkeypatch.setattr(extensions, "apply_headless", fake_apply_headless)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        f"[extensions]\n"
        f"unpacked = [\n"
        f"  {{ path = {json.dumps(str(ext_dir))}, "
        f'shortcuts = {{ _execute_action = "Control+KeyB" }} }},\n'
        f"]\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(cfg),
        dry_run=False,
        channel="stable",
    )
    brave_pkg.cmd_apply(args)
    assert headless_runs == ["Default"]

    # Advance disk mtimes so disk is authoritative, then change only the shortcut:
    # offline apply writes Preferences directly without launching headless Brave!
    future = time.time() + 5.0
    os.utime(sp_path, (future, future))
    os.utime(prefs_path, (future, future))

    headless_runs.clear()
    cfg.write_text(
        f"[extensions]\n"
        f"unpacked = [\n"
        f"  {{ path = {json.dumps(str(ext_dir))}, "
        f'shortcuts = {{ _execute_action = "Alt+KeyG" }} }},\n'
        f"]\n",
        encoding="utf-8",
    )
    brave_pkg.cmd_apply(args)
    assert headless_runs == []

    plat_key = extensions._command_platform_key()
    reloaded = json.loads(prefs_path.read_text(encoding="utf-8"))
    cmds = reloaded["extensions"]["commands"]
    assert f"{plat_key}:Ctrl+B" not in cmds
    assert cmds[f"{plat_key}:Alt+G"]["extension"] == ext_id


# ---------------------------------------------------------------------------
# Export & Undo
# ---------------------------------------------------------------------------


def test_export_emits_extensions_only_when_unpacked_present(
    profile_root: Path, tmp_path: Path
) -> None:
    prefs_path = profile_root / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert extensions.build_dump_block(prefs_path, prefs) is None

    ext_dir = _make_ext(tmp_path, "tabjump")
    ext_id = extensions.unpacked_extension_id(ext_dir.resolve())
    plat_key = extensions._command_platform_key()
    (profile_root / "Default" / "Secure Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        ext_id: {
                            "location": 4,
                            "path": str(ext_dir.resolve()),
                            "creation_flags": 38,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    prefs["extensions"] = {
        "commands": {
            f"{plat_key}:Ctrl+B": {
                "command_name": "_execute_action",
                "extension": ext_id,
                "global": False,
            }
        }
    }
    block = extensions.build_dump_block(prefs_path, prefs)
    assert block is not None
    parsed = tomllib.loads("\n".join(block) + "\n")
    assert parsed["extensions"]["unpacked"] == [
        {
            "path": str(ext_dir.resolve()),
            "shortcuts": {"_execute_action": "Control+KeyB"},
        }
    ]


def test_undo_clears_extensions_sidecar(
    profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    profile_dir = profile_root / "Default"
    prefs_path = profile_dir / "Preferences"
    (profile_dir / "Preferences.bak.20260101-000000.000001").write_text(
        prefs_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sidecar = extensions.state_file(prefs_path)
    sidecar.write_text(json.dumps({"managed": {}}), encoding="utf-8")

    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=None,
        undo=True,
        dry_run=False,
        channel="stable",
    )
    brave_pkg.cmd_apply(args)
    assert not sidecar.exists()
