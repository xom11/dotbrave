"""Tests for ``[extensions] webstore`` policy + live configuration management."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotbrave import browser as brave_pkg
from dotbrave import extensions, live, pwa, shields
from dotbrave._base import orchestrator as orch
from dotbrave._base import pwa as base_pwa


BITWARDEN_ID = "nngceckbapebfimnlniiiahkandclblb"
SURFINGKEYS_ID = "gfbliohnnapiefjpjlpjnehglfpaknnc"


def _make_ext(root: Path, name: str = "tabjump") -> Path:
    ext_dir = root / name
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": name,
                "version": "0.1.0",
                "commands": {
                    "_execute_action": {
                        "suggested_key": {"default": "Ctrl+B", "mac": "MacCtrl+B"}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (ext_dir / "popup.js").write_text("console.log('ok');\n", encoding="utf-8")
    return ext_dir


def _read_policy_file(path: Path) -> dict:
    if sys.platform == "darwin":
        with path.open("rb") as f:
            return plistlib.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    root = tmp_path / "profile_root"
    prof = root / "Default"
    prof.mkdir(parents=True)
    (prof / "Preferences").write_text(
        json.dumps({"brave": {"accelerators": {}}}), encoding="utf-8"
    )
    return root


@pytest.fixture
def policy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Redirect pwa, shields, and extensions policy storage into ``tmp_path``."""
    pdir = tmp_path / "policy"
    if sys.platform == "darwin":
        pwa_file = shields_file = ext_file = pdir / "com.brave.Browser.plist"
    else:
        pwa_file = pdir / "dotbrave-pwa.json"
        shields_file = pdir / "dotbrave-shields.json"
        ext_file = pdir / "dotbrave-extensions.json"

    monkeypatch.setattr(pwa, "POLICY_FILE", pwa_file)
    monkeypatch.setattr(shields, "POLICY_FILE", shields_file)
    monkeypatch.setattr(extensions, "POLICY_FILE", ext_file)
    writes: list[str] = []

    if sys.platform == "win32":
        import ctypes

        win_store: dict[str, Any] = {}

        def write_pwa(entries: list[dict]) -> None:
            writes.append("pwa")
            win_store[pwa.POLICY_KEY] = entries

        def write_shields(patterns: list[str]) -> None:
            writes.append("shields")
            win_store[shields.POLICY_KEY] = patterns

        def write_ext(mapping: dict[str, dict]) -> None:
            writes.append("extensions")
            if mapping:
                win_store[extensions.POLICY_KEY] = dict(mapping)
            else:
                win_store.pop(extensions.POLICY_KEY, None)

        monkeypatch.setattr(
            pwa,
            "_read_existing_payload",
            lambda: {pwa.POLICY_KEY: win_store.get(pwa.POLICY_KEY, [])},
        )
        monkeypatch.setattr(
            shields,
            "_read_current_policy",
            lambda: list(win_store.get(shields.POLICY_KEY, [])),
        )
        monkeypatch.setattr(
            extensions,
            "_read_current_policy",
            lambda: dict(win_store.get(extensions.POLICY_KEY, {})),
        )
        monkeypatch.setattr(pwa, "_sudo_write_policy", write_pwa)
        monkeypatch.setattr(shields, "_sudo_write_policy", write_shields)
        monkeypatch.setattr(extensions, "_sudo_write_policy", write_ext)
        monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    else:

        def write_pwa(entries: list[dict]) -> None:
            writes.append("pwa")
            pwa_file.parent.mkdir(parents=True, exist_ok=True)
            pwa_file.write_bytes(pwa._build_policy_payload(entries))

        def write_shields(patterns: list[str]) -> None:
            writes.append("shields")
            shields_file.parent.mkdir(parents=True, exist_ok=True)
            shields_file.write_bytes(
                base_pwa.build_policy_payload(
                    shields_file, "", patterns, shields.POLICY_KEY
                )
            )

        def write_ext(mapping: dict[str, dict]) -> None:
            writes.append("extensions")
            ext_file.parent.mkdir(parents=True, exist_ok=True)
            ext_file.write_bytes(
                base_pwa.build_policy_payload(
                    ext_file, "", mapping, extensions.POLICY_KEY
                )
            )

        monkeypatch.setattr(pwa, "_sudo_write_policy", write_pwa)
        monkeypatch.setattr(shields, "_sudo_write_policy", write_shields)
        monkeypatch.setattr(extensions, "_sudo_write_policy", write_ext)

        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if list(cmd[:3]) == ["sudo", "-n", "true"] or list(cmd[:2]) == [
                "sudo",
                "-v",
            ]:
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(orch.subprocess, "run", fake_run)

    monkeypatch.setattr(brave_pkg, "brave_running", lambda: False)
    return {
        "pwa": pwa_file,
        "shields": shields_file,
        "extensions": ext_file,
        "writes": writes,
    }


class _FakeExtCdpClient:
    def __init__(
        self,
        profile_dir: Path,
        installed_ids: set[str] | None = None,
        *,
        appear_after_polls: dict[str, int] | None = None,
        webstore_poll_responses: list[list[dict[str, Any]]] | None = None,
    ):
        self.profile_dir = str(profile_dir)
        self.installed_ids: set[str] = set(installed_ids or ())
        self._appear_after = dict(appear_after_polls or {})
        self._ws_poll_responses = list(webstore_poll_responses or [])
        self._info_polls = 0
        self._ws_info_polls = 0
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
        if "userMayModify: e.userMayModify" in expression:
            self._ws_info_polls += 1
            if self._ws_poll_responses:
                idx = min(self._ws_info_polls - 1, len(self._ws_poll_responses) - 1)
                return self._ws_poll_responses[idx]
            for eid, threshold in list(self._appear_after.items()):
                # Note: apply_via_cdp runs one initial _query_live_extension_ids
                # before _wait_for_webstore_ops, so count total polls across both.
                if (self._info_polls + self._ws_info_polls) >= threshold:
                    self.installed_ids.add(eid)
            return [{"id": eid} for eid in sorted(self.installed_ids)]
        if ").map(e => e.id)" in expression:
            self._info_polls += 1
            for eid, threshold in list(self._appear_after.items()):
                if (self._info_polls + self._ws_info_polls) >= threshold:
                    self.installed_ids.add(eid)
            return sorted(self.installed_ids)
        if "exts.some(e => e.id ===" in expression:
            return any(f'"{eid}"' in expression for eid in self.installed_ids)
        return True


def _apply(profile_root: Path, config: Path, **extra) -> None:
    args = argparse.Namespace(
        profile_root=profile_root,
        profile="Default",
        config=str(config),
        dry_run=False,
        channel="stable",
        **extra,
    )
    brave_pkg.cmd_apply(args)


# ---------------------------------------------------------------------------
# Validation of webstore & unpacked entries
# ---------------------------------------------------------------------------


def test_validate_webstore_entries_modes_pins_dedupe_and_platforms() -> None:
    validated = extensions.validate_extensions_table(
        {
            "webstore": [
                BITWARDEN_ID,
                {
                    "id": SURFINGKEYS_ID,
                    "name": "Surfingkeys",
                    "mode": "normal",
                    "pin": "default",
                    "file_access": True,
                    "incognito": True,
                    "shortcuts": {"_execute_action": "Alt+KeyS"},
                    "platforms": ["linux", "macos"],
                },
                # Duplicate after platform filtering: first wins.
                {"id": BITWARDEN_ID, "name": "Duplicate", "pin": "force"},
                # Filtered out on linux, but still validated.
                {
                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "pin": False,
                    "platforms": ["windows"],
                },
                {
                    "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "pin": True,
                    "platforms": ["linux"],
                },
                {
                    "id": "cccccccccccccccccccccccccccccccc",
                    "pin": "force",
                    "platforms": ["linux"],
                },
            ]
        },
        platform="linux",
    )
    assert validated.manage_webstore is True
    assert validated.manage_unpacked is False
    assert [e.ext_id for e in validated.webstore] == [
        BITWARDEN_ID,
        SURFINGKEYS_ID,
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "cccccccccccccccccccccccccccccccc",
    ]
    bw, sk, pin_true, pin_force = validated.webstore
    assert bw.policy_dict() == {
        "installation_mode": "force_installed",
        "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
    }
    assert sk.display_label == f"Surfingkeys ({SURFINGKEYS_ID})"
    assert "name" not in sk.policy_dict()
    assert sk.policy_dict() == {
        "installation_mode": "normal_installed",
        "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
        "toolbar_pin": "default_pinned",
    }
    assert sk.file_access is True
    assert sk.incognito is True
    assert sk.shortcuts == {"_execute_action": "Alt+S"}
    assert pin_true.toolbar_pin == "default_pinned"
    assert pin_force.toolbar_pin == "force_pinned"


def test_validate_subkey_presence_semantics(tmp_path: Path) -> None:
    ext_dir = _make_ext(tmp_path)

    empty = extensions.validate_extensions_table({})
    assert empty.manage_webstore is True and empty.webstore == []
    assert empty.manage_unpacked is True and empty.unpacked == []

    ws_only = extensions.validate_extensions_table({"webstore": [BITWARDEN_ID]})
    assert ws_only.manage_webstore is True and len(ws_only.webstore) == 1
    assert ws_only.manage_unpacked is False and ws_only.unpacked == []

    unpacked_only = extensions.validate_extensions_table(
        {"unpacked": [str(ext_dir)]}, config_dir=tmp_path
    )
    assert unpacked_only.manage_webstore is False and unpacked_only.webstore == []
    assert unpacked_only.manage_unpacked is True and len(unpacked_only.unpacked) == 1


@pytest.mark.parametrize(
    "raw, needle",
    [
        ({"other": []}, "unsupported keys"),
        ({"webstore": BITWARDEN_ID}, "webstore must be an array"),
        ({"webstore": [123]}, "strings or tables"),
        ({"webstore": ["short"]}, "invalid Chrome Web Store extension id"),
        (
            {"webstore": ["zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"]},
            "invalid Chrome Web Store extension id",
        ),
        (
            {"webstore": [f" {BITWARDEN_ID}"]},
            "surrounding whitespace",
        ),
        ({"webstore": [{"name": "No ID"}]}, "needs a string `id"),
        (
            {"webstore": [{"id": BITWARDEN_ID, "unknown": 1}]},
            "unsupported keys",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "name": " "}]},
            "name must be a non-empty string",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "name": " Padded "}]},
            "surrounding whitespace",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "mode": "blocked"}]},
            "mode must be 'force' or 'normal'",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "pin": 1}]},
            "pin must be",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "pin": "always"}]},
            "pin must be",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "file_access": "yes"}]},
            "file_access must be a boolean",
        ),
        (
            {"webstore": [{"id": BITWARDEN_ID, "incognito": 1}]},
            "incognito must be a boolean",
        ),
        # Shortcut syntax is validated BEFORE platform filtering.
        (
            {
                "webstore": [
                    {
                        "id": BITWARDEN_ID,
                        "shortcuts": {"_execute_action": "Meta+KeyB"},
                        "platforms": ["linux"],
                    }
                ]
            },
            "only support on macOS",
        ),
    ],
)
def test_validate_webstore_rejects_invalid(raw: object, needle: str) -> None:
    with pytest.raises(SystemExit) as e:
        extensions.validate_extensions_table(raw, platform="macos")
    assert needle in str(e.value)


# ---------------------------------------------------------------------------
# Policy storage: MANAGED_POLICY_KEYS, macOS merge + daemon, Linux JSON, Windows REG_SZ dict
# ---------------------------------------------------------------------------


def test_pwa_managed_policy_keys_includes_extension_settings() -> None:
    assert extensions.POLICY_KEY in pwa.MANAGED_POLICY_KEYS


def test_default_policy_file_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extensions.sys, "platform", "linux")
    assert extensions._default_policy_file() == Path(
        "/etc/brave/policies/managed/dotbrave-extensions.json"
    )
    monkeypatch.setattr(extensions.sys, "platform", "darwin")
    assert extensions._default_policy_file() == Path(
        "/Library/Managed Preferences/com.brave.Browser.plist"
    )
    monkeypatch.setattr(extensions.sys, "platform", "win32")
    assert extensions._default_policy_file() is None


def test_windows_registry_dict_read_write_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ExtensionSettings`` on Windows is a single ``REG_SZ`` JSON object on
    ``HKLM\\Software\\Policies\\BraveSoftware\\Brave``, NOT numbered subkey values."""

    class FakeWinreg:
        HKEY_LOCAL_MACHINE = 0x80000002
        KEY_READ = 0x20019
        KEY_WRITE = 0x20006
        REG_SZ = 1

        def __init__(self) -> None:
            self.keys: dict[tuple[int, str], dict[str, tuple[int, Any]]] = {}

        def CreateKeyEx(self, root: int, subkey: str, _res: int, _acc: int):
            self.keys.setdefault((root, subkey), {})
            return (root, subkey)

        def OpenKey(self, root: int, subkey: str, _res: int, _acc: int):
            if (root, subkey) not in self.keys:
                raise FileNotFoundError(subkey)
            return (root, subkey)

        def SetValueEx(
            self, handle, name: str, _res: int, vtype: int, value: Any
        ) -> None:
            self.keys[handle][name] = (vtype, value)

        def QueryValueEx(self, handle, name: str):
            if name not in self.keys[handle]:
                raise FileNotFoundError(name)
            vtype, value = self.keys[handle][name]
            return value, vtype

        def DeleteValue(self, handle, name: str) -> None:
            if name not in self.keys[handle]:
                raise FileNotFoundError(name)
            del self.keys[handle][name]

        def CloseKey(self, _handle) -> None:
            pass

    fake_reg = FakeWinreg()
    monkeypatch.setattr(base_pwa, "winreg", fake_reg, raising=False)
    monkeypatch.setattr(base_pwa.sys, "platform", "win32")

    reg_path = r"Software\Policies\BraveSoftware\Brave"
    assert base_pwa.read_windows_registry_dict(reg_path, "ExtensionSettings") == {}

    sample = {
        SURFINGKEYS_ID: {
            "installation_mode": "force_installed",
            "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
        },
        BITWARDEN_ID: {
            "installation_mode": "normal_installed",
            "toolbar_pin": "default_pinned",
            "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
        },
    }
    base_pwa.sudo_write_policy(
        None,
        reg_path,
        sample,
        policy_key="ExtensionSettings",
        managed_keys=pwa.MANAGED_POLICY_KEYS,
    )

    stored_type, stored_raw = fake_reg.keys[(FakeWinreg.HKEY_LOCAL_MACHINE, reg_path)][
        "ExtensionSettings"
    ]
    assert stored_type == FakeWinreg.REG_SZ
    assert json.loads(stored_raw) == sample
    assert (
        base_pwa.read_windows_registry_dict(reg_path, "ExtensionSettings") == sample
    )

    # Emptying deletes the REG_SZ value cleanly (and is idempotent).
    base_pwa.write_windows_registry_dict(reg_path, {}, "ExtensionSettings")
    assert "ExtensionSettings" not in fake_reg.keys[
        (FakeWinreg.HKEY_LOCAL_MACHINE, reg_path)
    ]
    assert base_pwa.read_windows_registry_dict(reg_path, "ExtensionSettings") == {}
    base_pwa.write_windows_registry_dict(reg_path, {}, "ExtensionSettings")


def test_macos_emptying_pwa_keeps_daemon_while_extension_settings_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(base_pwa.sys, "platform", "darwin")
    monkeypatch.setattr(
        base_pwa.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0),
    )
    installed: list[bytes] = []
    removed: list[Path] = []
    monkeypatch.setattr(
        base_pwa,
        "install_self_healing_daemon",
        lambda _pf, content: installed.append(content),
    )
    monkeypatch.setattr(
        base_pwa, "remove_self_healing_daemon", lambda pf: removed.append(pf)
    )
    monkeypatch.setattr(base_pwa, "notify_darwin_policy_changed", lambda _pf: None)

    policy_file = tmp_path / "com.brave.Browser.plist"
    policy_file.write_bytes(
        plistlib.dumps(
            {
                pwa.POLICY_KEY: [{"url": "https://squoosh.app/"}],
                extensions.POLICY_KEY: {
                    SURFINGKEYS_ID: {
                        "installation_mode": "force_installed",
                        "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
                    }
                },
            }
        )
    )
    base_pwa.sudo_write_policy(
        policy_file, "", [], managed_keys=pwa.MANAGED_POLICY_KEYS
    )

    assert removed == []
    assert len(installed) == 1
    merged = plistlib.loads(installed[0])
    assert merged[pwa.POLICY_KEY] == []
    assert SURFINGKEYS_ID in merged[extensions.POLICY_KEY]


def test_pwa_shields_and_webstore_extensions_all_coexist_in_one_apply(
    profile_root: Path, policy_env: dict, tmp_path: Path
) -> None:
    if sys.platform == "win32":
        pytest.skip("file-backed policy coexistence test is for Linux/macOS")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        '[pwa]\nurls = ["https://squoosh.app/"]\n\n'
        '[shields]\ndisabled = ["[*.]messenger.com"]\n\n'
        "[extensions]\n"
        f'webstore = [{{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", pin = "default" }}]\n',
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    assert [e["url"] for e in _read_policy_file(policy_env["pwa"])[pwa.POLICY_KEY]] == [
        "https://squoosh.app/"
    ]
    assert _read_policy_file(policy_env["shields"])[shields.POLICY_KEY] == [
        "[*.]messenger.com"
    ]
    assert _read_policy_file(policy_env["extensions"])[extensions.POLICY_KEY] == {
        SURFINGKEYS_ID: {
            "installation_mode": "force_installed",
            "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
            "toolbar_pin": "default_pinned",
        }
    }


# ---------------------------------------------------------------------------
# Sub-key isolation: unpacked-only vs webstore-only vs bare [extensions]
# ---------------------------------------------------------------------------


def test_subkey_isolation_and_empty_table_reset(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    unpacked_id = extensions.unpacked_extension_id(ext_dir.resolve())

    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default", installed_ids={SURFINGKEYS_ID}
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    # 1. Install both webstore (with shortcut) and unpacked.
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        f'webstore = [{{ id = "{SURFINGKEYS_ID}", shortcuts = {{ _execute_action = "Alt+KeyS" }} }}]\n'
        f"unpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)
    assert SURFINGKEYS_ID in extensions._read_current_policy()
    assert unpacked_id in fake_cdp.installed_ids

    # 2. Apply unpacked-only config WITH NO SUDO AVAILABLE: must NOT read/write
    # webstore policy, must NOT escalate, and must preserve webstore_prefs in sidecar!
    policy_env["writes"].clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orch, "_already_privileged", lambda: False)
        real_run = subprocess.run

        def forbid_sudo(cmd, *a, **k):
            if list(cmd)[:1] == ["sudo"]:
                raise AssertionError("unpacked-only config must never invoke sudo")
            return real_run(cmd, *a, **k)

        mp.setattr(orch.subprocess, "run", forbid_sudo)
        mp.setattr(
            extensions,
            "_read_current_policy",
            lambda: (_ for _ in ()).throw(
                AssertionError("unpacked-only config must not read webstore policy")
            ),
        )
        (ext_dir / "popup.js").write_text("console.log('v2');\n", encoding="utf-8")
        cfg.write_text(
            f"[extensions]\nunpacked = [{json.dumps(str(ext_dir))}]\n",
            encoding="utf-8",
        )
        _apply(profile_root, cfg)

    assert policy_env["writes"] == []
    assert SURFINGKEYS_ID in extensions._read_current_policy()
    sidecar_data = json.loads(
        extensions.state_file(profile_root / "Default" / "Preferences").read_text(
            encoding="utf-8"
        )
    )
    assert SURFINGKEYS_ID in sidecar_data["webstore_prefs"]

    # 3. Apply webstore-only config: must NOT uninstall the unpacked extension!
    fake_cdp.browser_calls.clear()
    cfg.write_text(
        "[extensions]\n"
        f'webstore = ["{BITWARDEN_ID}", {{ id = "{SURFINGKEYS_ID}", shortcuts = {{ _execute_action = "Alt+KeyS" }} }}]\n',
        encoding="utf-8",
    )
    _apply(profile_root, cfg)
    assert not any(m == "Extensions.uninstall" for m, _ in fake_cdp.browser_calls)
    assert unpacked_id in fake_cdp.installed_ids
    sidecar_data = json.loads(
        extensions.state_file(profile_root / "Default" / "Preferences").read_text(
            encoding="utf-8"
        )
    )
    assert unpacked_id in sidecar_data["managed"]

    # 4. Bare `[extensions]` header resets BOTH webstore policy and unpacked!
    fake_cdp.browser_calls.clear()
    fake_cdp.evaluations.clear()
    cfg.write_text("[extensions]\n", encoding="utf-8")
    _apply(profile_root, cfg)
    assert extensions._read_current_policy() == {}
    assert ("Extensions.uninstall", {"id": unpacked_id}) in fake_cdp.browser_calls
    assert any(
        "updateExtensionCommand" in expr
        and SURFINGKEYS_ID in expr
        and 'keybinding: ""' in expr
        for expr in fake_cdp.evaluations
    )


# ---------------------------------------------------------------------------
# Running Brave: pure webstore (zero CDP) vs webstore + permissions/shortcuts
# ---------------------------------------------------------------------------


def test_running_brave_pure_webstore_applies_without_touching_browser(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda: (_ for _ in ()).throw(AssertionError("must not close Brave")),
    )

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        f'webstore = [{{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys" }}]\n',
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    out = capsys.readouterr().out
    assert f"+ Surfingkeys ({SURFINGKEYS_ID}) (force_installed)" in out
    assert "[extensions] applied without touching the running Brave" in out
    assert not extensions.state_file(
        profile_root / "Default" / "Preferences"
    ).exists()


def test_running_brave_webstore_polls_until_installed_then_applies_file_access_incognito_and_shortcuts(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    orig_write = extensions._sudo_write_policy

    def tracked_write(mapping: dict[str, dict]) -> None:
        events.append("policy_write")
        orig_write(mapping)

    monkeypatch.setattr(extensions, "_sudo_write_policy", tracked_write)
    monkeypatch.setattr(extensions, "_WEBSTORE_INSTALL_WAIT_S", 1.0)
    monkeypatch.setattr(extensions, "_WEBSTORE_INSTALL_POLL_S", 0.005)

    # Surfingkeys finishes CWS download on the 3rd getExtensionsInfo() poll.
    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default",
        appear_after_polls={SURFINGKEYS_ID: 3},
    )
    orig_eval = fake_cdp.evaluate

    def tracked_eval(target: dict, expr: str):
        if "updateExtensionConfiguration" in expr:
            events.append("cdp_config")
        elif "updateExtensionCommand" in expr:
            events.append("cdp_shortcut")
        return orig_eval(target, expr)

    monkeypatch.setattr(fake_cdp, "evaluate", tracked_eval)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        "webstore = [\n"
        f'  {{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", '
        'file_access = true, incognito = true, shortcuts = { _execute_action = "Alt+KeyS" } },\n'
        "]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    out = capsys.readouterr().out
    assert out.count("extensions:") == 1
    assert events[0] == "policy_write"
    assert "cdp_config" in events and "cdp_shortcut" in events
    assert any(
        "incognitoAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )
    assert any(
        "fileAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )
    assert any(
        "updateExtensionCommand" in expr
        and SURFINGKEYS_ID in expr
        and '"Alt+S"' in expr
        for expr in fake_cdp.evaluations
    )

    # Immediate re-apply inside commit window is a complete no-op.
    events.clear()
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    assert "no changes" in capsys.readouterr().out
    assert events == []


def test_webstore_timeout_warns_and_keeps_state_unrecorded_for_retry(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When CWS has not finished downloading (e.g. offline), warn on stderr
    and omit the unapplied entry from ``webstore_prefs`` so the next apply retries."""
    monkeypatch.setattr(extensions, "_WEBSTORE_INSTALL_WAIT_S", 0.0)
    fake_cdp = _FakeExtCdpClient(profile_root / "Default", installed_ids=set())
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        f'webstore = [{{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", file_access = true }}]\n',
        encoding="utf-8",
    )
    _apply(profile_root, cfg)
    captured = capsys.readouterr()
    assert "warning: [extensions]" in captured.err
    assert "Surfingkeys" in captured.err

    sidecar = json.loads(
        extensions.state_file(profile_root / "Default" / "Preferences").read_text(
            encoding="utf-8"
        )
    )
    assert "webstore_prefs" not in sidecar

    # Next apply once CWS finishes downloading retries file_access = true!
    fake_cdp.installed_ids.add(SURFINGKEYS_ID)
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    assert any(
        "fileAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )


def test_mixed_extensions_plans_survive_live_relaunch_and_unattended_skip(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two plans sharing ``namespace == "extensions"`` must not collide in
    ``orchestrator``'s post-relaunch ``rebuilt`` dict or duplicate ``[extensions]``
    in ``--unattended`` privilege-skip warnings."""
    ext_dir = _make_ext(tmp_path, "tabjump")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        f'webstore = ["{BITWARDEN_ID}"]\n'
        f"unpacked = [{json.dumps(str(ext_dir))}]\n",
        encoding="utf-8",
    )

    fake_cdp = _FakeExtCdpClient(profile_root / "Default")
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)

    # 1. --unattended while unprivileged skips ONLY the webstore policy plan
    # and still applies unpacked live!
    monkeypatch.setattr(orch, "_already_privileged", lambda: False)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)
    _apply(profile_root, cfg, unattended=True)
    err = capsys.readouterr().err
    assert "skipping [extensions]" in err
    assert extensions._read_current_policy() == {}
    assert any(m == "Extensions.loadUnpacked" for m, _ in fake_cdp.browser_calls)

    # 2. Attended run with NO live endpoint: applies external webstore policy
    # once, closes + relaunches Brave, rebuilds plans without re-adding the
    # external plan, and finishes browser-bound changes cleanly.
    (ext_dir / "popup.js").write_text("console.log('v2');\n", encoding="utf-8")
    fake_cdp.browser_calls.clear()
    fake_cdp.evaluations.clear()
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(orch, "pick_unused_port", lambda: 9444)
    monkeypatch.setattr(orch, "wait_for_devtools_endpoint", lambda p, n: None)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(brave_pkg.BROWSER_PROCESS, "close_and_wait", lambda: None)
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS, "launch_live", lambda *a, **k: ["brave"]
    )
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS, "relaunch_blocker", lambda: None
    )

    policy_env["writes"].clear()
    _apply(profile_root, cfg, unattended=False)
    assert policy_env["writes"] == ["extensions"]
    assert BITWARDEN_ID in extensions._read_current_policy()
    assert any("chrome.developerPrivate.reload" in e for e in fake_cdp.evaluations)


# ---------------------------------------------------------------------------
# Export round-trip for webstore + unpacked
# ---------------------------------------------------------------------------


def test_export_round_trips_webstore_and_unpacked(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    unpacked_id = extensions.unpacked_extension_id(ext_dir.resolve())
    plat_key = extensions._command_platform_key()

    extensions._sudo_write_policy(
        {
            BITWARDEN_ID: {
                "installation_mode": "force_installed",
                "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
            },
            SURFINGKEYS_ID: {
                "installation_mode": "normal_installed",
                "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
                "toolbar_pin": "default_pinned",
            },
        }
    )

    prefs_path = profile_root / "Default" / "Preferences"
    sp_path = profile_root / "Default" / "Secure Preferences"
    sp_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        SURFINGKEYS_ID: {
                            "location": 1,
                            "incognito": True,
                            "newAllowFileAccess": True,
                        },
                        unpacked_id: {
                            "location": 4,
                            "path": str(ext_dir.resolve()),
                            "creation_flags": 38,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    prefs = {
        "extensions": {
            "commands": {
                f"{plat_key}:Alt+S": {
                    "command_name": "_execute_action",
                    "extension": SURFINGKEYS_ID,
                    "global": False,
                },
                f"{plat_key}:Ctrl+B": {
                    "command_name": "_execute_action",
                    "extension": unpacked_id,
                    "global": False,
                },
            }
        }
    }
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    # Seed sidecar as if dotbrave already owns unpacked so round-trip is diff-free.
    sidecar = extensions.state_file(prefs_path)
    sidecar.write_text(
        json.dumps(
            {
                "managed": {
                    unpacked_id: {
                        "path": str(ext_dir.resolve()),
                        "display_path": str(ext_dir.resolve()),
                        "source_sha256": extensions.hash_extension_dir(
                            ext_dir.resolve()
                        ),
                        "shortcuts": {"_execute_action": "Ctrl+B"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    future = time.time() + 5.0
    os.utime(sp_path, (future, future))
    os.utime(prefs_path, (future, future))

    block = extensions.build_dump_block(prefs_path, prefs)
    assert block is not None
    parsed = tomllib.loads("\n".join(block) + "\n")
    assert parsed["extensions"]["webstore"] == [
        {
            "id": SURFINGKEYS_ID,
            "mode": "normal",
            "pin": "default",
            "file_access": True,
            "incognito": True,
            "shortcuts": {"_execute_action": "Alt+KeyS"},
        },
        BITWARDEN_ID,
    ]
    assert parsed["extensions"]["unpacked"] == [
        {
            "path": str(ext_dir.resolve()),
            "shortcuts": {"_execute_action": "Control+KeyB"},
        }
    ]

    plans = extensions.plan_apply_all(
        prefs_path, prefs, parsed["extensions"], config_dir=tmp_path
    )
    assert all(p.empty for p in plans)


def test_unpacked_file_access_and_snap_sandbox_refusal_only_when_webstore_managed(
    profile_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ext_dir = _make_ext(tmp_path, "tabjump")
    fake_cdp = _FakeExtCdpClient(profile_root / "Default")
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        f"unpacked = [{{ path = {json.dumps(str(ext_dir))}, file_access = false }}]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)
    assert any("fileAccess: false" in expr for expr in fake_cdp.evaluations)

    # Snap sandbox path refuses webstore policy, but allows unpacked-only plan building.
    snap_prefs = (
        tmp_path
        / "snap"
        / "brave"
        / "current"
        / ".config"
        / "BraveSoftware"
        / "Brave-Browser"
        / "Default"
        / "Preferences"
    )
    with pytest.raises(SystemExit, match="Snap Brave"):
        extensions.plan_apply_all(
            snap_prefs, {}, {"webstore": [BITWARDEN_ID]}, config_dir=tmp_path
        )
    plans = extensions.plan_apply_all(
        snap_prefs, {}, {"unpacked": [str(ext_dir)]}, config_dir=tmp_path
    )
    assert len(plans) == 1 and plans[0].external_apply_fn is None


def test_extensions_external_policy_triggers_live_cdp_reload_on_linux_and_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "brave.toml"
    cfg.write_text(f'[extensions]\nwebstore = ["{BITWARDEN_ID}"]\n', encoding="utf-8")

    reloaded: list[bool] = []
    monkeypatch.setattr(orch.sys, "platform", "win32")
    monkeypatch.setattr(orch, "_already_privileged", lambda: True)
    monkeypatch.setattr(
        orch,
        "_reload_external_policy_live",
        lambda *_a: reloaded.append(True) or True,
    )

    orch.cmd_apply(
        argparse.Namespace(
            profile_root=tmp_path,
            profile="Default",
            config=str(cfg),
            dry_run=False,
            unattended=False,
        ),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave.exe", "--remote-debugging-port=9222"],
        restart_fn=lambda c: c,
        build_plans_fn=lambda p, prefs, doc, **kw: [
            orch.Plan(
                namespace="extensions",
                diff_lines=[f"  + {BITWARDEN_ID} (force_installed)"],
                apply_fn=lambda _p: None,
                verify_fn=lambda _p: None,
                external_apply_fn=lambda: None,
            )
        ],
        live_apply_fn=lambda *a, **k: None,
        graceful_close_fn=lambda: None,
        launch_live_fn=lambda *a, **k: [],
    )
    out = capsys.readouterr().out
    assert reloaded == [True]
    assert "[extensions] applied without touching the running Brave" in out
    assert "picks it up within a few seconds" in out


def test_mode_downgrade_force_to_normal_waits_for_modifiable_and_reapplies_permissions(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Downgrading ``force_installed`` -> ``normal_installed`` makes Chromium
    uninstall the force-installed extension and reinstall a fresh normal copy
    with ``fileAccess = false`` and ``incognitoAccess = false``, while
    ``Secure Preferences`` on disk still holds ``True`` from before the policy
    write and ``getExtensionsInfo()`` briefly reports the outgoing
    ``userMayModify: false`` instance before it flips to ``userMayModify: true``."""
    monkeypatch.setattr(extensions, "_WEBSTORE_INSTALL_WAIT_S", 1.0)
    monkeypatch.setattr(extensions, "_WEBSTORE_INSTALL_POLL_S", 0.005)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    # Pre-seed current policy as `force_installed` and Secure Preferences with
    # `incognito: True` and `newAllowFileAccess: True`.
    extensions._sudo_write_policy(
        {
            SURFINGKEYS_ID: {
                "installation_mode": "force_installed",
                "update_url": extensions.CHROME_WEBSTORE_UPDATE_URL,
            }
        }
    )
    sp_path = profile_root / "Default" / "Secure Preferences"
    sp_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        SURFINGKEYS_ID: {
                            "location": 8,
                            "incognito": True,
                            "newAllowFileAccess": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # Poll 1: outgoing force-installed copy (`userMayModify: False`).
    # Poll 2: mid-transition uninstalled (`[]`).
    # Poll 3: newly reinstalled normal copy (`userMayModify: True`).
    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default",
        installed_ids={SURFINGKEYS_ID},
        webstore_poll_responses=[
            [{"id": SURFINGKEYS_ID, "userMayModify": False}],
            [],
            [{"id": SURFINGKEYS_ID, "userMayModify": True}],
        ],
    )
    poll_count_at_config: list[int] = []
    orig_eval = fake_cdp.evaluate

    def tracked_eval(target: dict, expr: str):
        if "updateExtensionConfiguration" in expr:
            poll_count_at_config.append(fake_cdp._ws_info_polls)
        return orig_eval(target, expr)

    monkeypatch.setattr(fake_cdp, "evaluate", tracked_eval)
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        "webstore = [\n"
        f'  {{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", mode = "normal", '
        "file_access = true, incognito = true },\n"
        "]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    assert extensions._read_current_policy()[SURFINGKEYS_ID]["installation_mode"] == (
        "normal_installed"
    )
    assert fake_cdp._ws_info_polls == 3
    assert poll_count_at_config == [3, 3]
    assert any(
        "incognitoAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )
    assert any(
        "fileAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )

    # Subsequent apply with `mode = "normal"` already in policy is a no-op.
    capsys.readouterr()
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    assert "no changes" in capsys.readouterr().out
    assert fake_cdp.evaluations == []


def test_readding_webstore_over_stale_secure_preferences_still_configures_cdp(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``webstore = []`` uninstalled an extension moments ago, on-disk
    ``Secure Preferences`` can still report ``incognito: True`` and
    ``newAllowFileAccess: True`` for ~10s while ``current_policy`` is empty.
    Re-adding the extension must still run ``updateExtensionConfiguration``."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    assert extensions._read_current_policy() == {}
    sp_path = profile_root / "Default" / "Secure Preferences"
    sp_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        SURFINGKEYS_ID: {
                            "location": 8,
                            "incognito": True,
                            "newAllowFileAccess": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default", installed_ids={SURFINGKEYS_ID}
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        "webstore = [\n"
        f'  {{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", '
        "file_access = true, incognito = true },\n"
        "]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    assert any(
        "incognitoAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )
    assert any(
        "fileAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )

    # Immediate re-apply is a no-op now that SURFINGKEYS_ID is in current_policy.
    capsys.readouterr()
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    assert "no changes" in capsys.readouterr().out
    assert fake_cdp.evaluations == []


def test_adopting_existing_sync_webstore_extension_skips_redundant_reapply(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adopting an already-installed Sync/Web Store extension (``location == 1``)
    into ``ExtensionSettings`` upgrades ``location`` in place and preserves
    ``incognito`` / ``newAllowFileAccess`` / ``user_scripts_enabled``, so a run
    whose desired permissions already match on disk must NOT emit ``(reapply)``
    or touch CDP / restart Brave."""
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: None)
    monkeypatch.setattr(
        brave_pkg.BROWSER_PROCESS,
        "close_and_wait",
        lambda: (_ for _ in ()).throw(AssertionError("must not close Brave")),
    )

    sp_path = profile_root / "Default" / "Secure Preferences"
    sp_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        SURFINGKEYS_ID: {
                            "location": 1,
                            "incognito": True,
                            "newAllowFileAccess": True,
                            "user_scripts_enabled": True,
                        },
                        # Uninstalled tombstone (`location` absent) must NOT be
                        # mistaken for an installed extension with `incognito: True`.
                        BITWARDEN_ID: {
                            "incognito": True,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = tmp_path / "brave.toml"
    cfg.write_text(
        "[extensions]\n"
        "webstore = [\n"
        f'  {{ id = "{SURFINGKEYS_ID}", name = "Surfingkeys", mode = "normal", '
        "file_access = true, incognito = true, user_scripts = true },\n"
        "]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    out = capsys.readouterr().out
    assert "(reapply)" not in out
    assert "[extensions] applied without touching the running Brave" in out
    sidecar = json.loads(
        extensions.state_file(profile_root / "Default" / "Preferences").read_text(
            encoding="utf-8"
        )
    )
    assert sidecar["webstore_prefs"][SURFINGKEYS_ID] == {
        "incognito": True,
        "file_access": True,
        "user_scripts": True,
    }

    # Verify tombstone entry (`location: None`) still produces `False -> True` diff.
    plans = extensions.plan_apply_all(
        profile_root / "Default" / "Preferences",
        {},
        {"webstore": [{"id": BITWARDEN_ID, "incognito": True}]},
    )
    assert any("False -> True" in line for p in plans for line in p.diff_lines)


def test_path_to_file_url_posix_and_windows() -> None:
    assert (
        extensions.path_to_file_url(
            "/Users/lenamkhanh/.nix/home-manager/dotfiles/browser/surfingkeys/configs.js",
            platform="darwin",
        )
        == "file:///Users/lenamkhanh/.nix/home-manager/dotfiles/browser/surfingkeys/configs.js"
    )
    assert (
        extensions.path_to_file_url(
            "/home/kln/.nix/home-manager/dotfiles/browser/surfingkeys/configs.js",
            platform="linux",
        )
        == "file:///home/kln/.nix/home-manager/dotfiles/browser/surfingkeys/configs.js"
    )
    assert (
        extensions.path_to_file_url(
            r"c:\Users\kln\.nix\home-manager\dotfiles\browser\surfingkeys\configs.js",
            platform="win32",
        )
        == "file:///C:/Users/kln/.nix/home-manager/dotfiles/browser/surfingkeys/configs.js"
    )
    assert (
        extensions.path_to_file_url(
            r"\\?\c:\Users\kln\.nix\surfingkeys\my configs#1.js",
            platform="win32",
        )
        == "file:///C:/Users/kln/.nix/surfingkeys/my%20configs%231.js"
    )
    assert (
        extensions.path_to_file_url(
            r"\\?\UNC\nas\dotfiles\configs.js",
            platform="win32",
        )
        == "file://nas/dotfiles/configs.js"
    )


@pytest.mark.parametrize(
    "raw, needle",
    [
        (
            {"webstore": [{"id": SURFINGKEYS_ID, "user_scripts": "true"}]},
            "user_scripts must be a boolean",
        ),
        (
            {"webstore": [{"id": SURFINGKEYS_ID, "storage": ["bad"]}]},
            "storage must be a table",
        ),
        (
            {"webstore": [{"id": SURFINGKEYS_ID, "storage": {"": 1}}]},
            "storage key must be a non-empty string",
        ),
        (
            {
                "webstore": [
                    {
                        "id": SURFINGKEYS_ID,
                        "storage": {"localPath": {"file_url": "   "}},
                    }
                ]
            },
            "file_url in storage.localPath must be a non-empty string",
        ),
        (
            {
                "webstore": [
                    {
                        "id": SURFINGKEYS_ID,
                        "storage": {"localPath": {"file_url": " padded.js "}},
                    }
                ]
            },
            "surrounding whitespace",
        ),
        (
            {"webstore": [{"id": SURFINGKEYS_ID, "messages": {"action": "x"}}]},
            "messages must be an array",
        ),
        (
            {
                "webstore": [
                    {"id": SURFINGKEYS_ID, "messages": [{"file_url": "a.js"}]}
                ]
            },
            "messages[0] must be a message table",
        ),
    ],
)
def test_validate_user_scripts_storage_messages_rejects_invalid(
    raw: object, needle: str
) -> None:
    with pytest.raises(SystemExit) as e:
        extensions.validate_extensions_table(raw, platform="macos")
    assert needle in str(e.value)


def test_validate_file_url_missing_remote_and_platform_skip(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e_missing:
        extensions.validate_extensions_table(
            {
                "webstore": [
                    {
                        "id": SURFINGKEYS_ID,
                        "storage": {"localPath": {"file_url": "../nope.js"}},
                    }
                ]
            },
            config_dir=tmp_path,
            platform="macos",
        )
    assert "file_url file not found" in str(e_missing.value)

    with pytest.raises(SystemExit) as e_remote:
        extensions.validate_extensions_table(
            {
                "webstore": [
                    {
                        "id": SURFINGKEYS_ID,
                        "storage": {"localPath": {"file_url": "../configs.js"}},
                    }
                ]
            },
            config_dir=None,
            platform="macos",
        )
    assert "local TOML file" in str(e_remote.value)

    # Non-matching platform skips file existence check after validating syntax.
    skipped = extensions.validate_extensions_table(
        {
            "webstore": [
                {
                    "id": SURFINGKEYS_ID,
                    "storage": {"localPath": {"file_url": "/nonexistent/win.js"}},
                    "platforms": ["windows"],
                }
            ]
        },
        config_dir=tmp_path,
        platform="linux",
    )
    assert skipped.webstore == []


def test_webstore_user_scripts_storage_messages_file_url_edit_removal_and_export(
    profile_root: Path,
    policy_env: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser_dir = tmp_path / "browser"
    cfg_dir = browser_dir / "dotbrave"
    sk_dir = browser_dir / "surfingkeys"
    cfg_dir.mkdir(parents=True)
    sk_dir.mkdir(parents=True)
    configs_js = sk_dir / "configs.js"
    configs_js.write_text('api.map("J", "R");\n', encoding="utf-8")
    expected_uri = extensions.path_to_file_url(configs_js.resolve())

    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default", installed_ids={SURFINGKEYS_ID}
    )
    monkeypatch.setattr(live, "CdpClient", lambda port: fake_cdp)
    monkeypatch.setattr(brave_pkg, "brave_running", lambda: True)
    monkeypatch.setattr(brave_pkg, "find_main_brave_cmdline", lambda: ["brave"])
    monkeypatch.setattr(orch, "find_devtools_port", lambda r, p: 9222)
    monkeypatch.setattr(orch, "remember_devtools_port", lambda r, p, port: None)

    cfg = cfg_dir / "brave.toml"
    cfg.write_text(
        "[[extensions.webstore]]\n"
        f'id = "{SURFINGKEYS_ID}"\n'
        'name = "Surfingkeys"\n'
        'mode = "normal"\n'
        "file_access = true\n"
        "incognito = true\n"
        "user_scripts = true\n"
        'storage = { showAdvanced = true, obsoleteKey = "v1", localPath = { file_url = "../surfingkeys/configs.js" } }\n'
        "messages = [\n"
        '  { action = "updateSettings", needResponse = true, settings = { showAdvanced = true } },\n'
        '  { action = "loadSettingsFromUrl", needResponse = true, url = { file_url = "../surfingkeys/configs.js" } },\n'
        "]\n",
        encoding="utf-8",
    )
    _apply(profile_root, cfg)

    assert any(
        "userScriptsAccess: true" in expr and SURFINGKEYS_ID in expr
        for expr in fake_cdp.evaluations
    )
    assert f"chrome-extension://{SURFINGKEYS_ID}/manifest.json" in fake_cdp.navigations
    assert any(
        "chrome.storage.local.set(" in expr and expected_uri in expr
        for expr in fake_cdp.evaluations
    )
    msg_evals = [
        expr for expr in fake_cdp.evaluations if "chrome.runtime.sendMessage(" in expr
    ]
    assert len(msg_evals) == 2
    assert '"updateSettings"' in msg_evals[0]
    assert '"loadSettingsFromUrl"' in msg_evals[1] and expected_uri in msg_evals[1]

    # Seed Secure Preferences after flush (without user_scripts_enabled, as on
    # Chromium builds storing user_scripts in StateStore LevelDB) and verify
    # steady-state re-apply is a 0-CDP no-op!
    prefs_path = profile_root / "Default" / "Preferences"
    sp_path = profile_root / "Default" / "Secure Preferences"
    sp_path.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        SURFINGKEYS_ID: {
                            "location": 6,
                            "incognito": True,
                            "newAllowFileAccess": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    future = time.time() + 5.0
    os.utime(sp_path, (future, future))
    os.utime(prefs_path, (future, future))

    capsys.readouterr()
    fake_cdp.evaluations.clear()
    fake_cdp.navigations.clear()
    _apply(profile_root, cfg)
    assert "no changes" in capsys.readouterr().out
    assert fake_cdp.evaluations == []

    # Export round-trips user_scripts, storage, and messages cleanly with 0 diff!
    block = extensions.build_dump_block(prefs_path, {})
    assert block is not None
    exported_parsed = tomllib.loads("\n".join(block) + "\n")
    roundtrip_plans = extensions.plan_apply_all(
        prefs_path, {}, exported_parsed["extensions"], config_dir=cfg_dir
    )
    assert all(p.empty for p in roundtrip_plans)

    # Editing configs.js on disk updates SHA-256 and triggers re-applying storage + messages!
    configs_js.write_text('api.map("J", "R");\napi.map("K", "E");\n', encoding="utf-8")
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    out = capsys.readouterr().out
    assert "file_url updated" in out
    assert any("chrome.storage.local.set(" in expr for expr in fake_cdp.evaluations)
    assert (
        sum(
            1
            for expr in fake_cdp.evaluations
            if "chrome.runtime.sendMessage(" in expr
        )
        == 2
    )

    # Dropping `obsoleteKey` from storage removes it via chrome.storage.local.remove.
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(', obsoleteKey = "v1"', ""),
        encoding="utf-8",
    )
    fake_cdp.evaluations.clear()
    _apply(profile_root, cfg)
    assert any(
        'chrome.storage.local.remove(["obsoleteKey"])' in expr
        for expr in fake_cdp.evaluations
    )


def test_extension_message_failure_raises_cdp_error(
    profile_root: Path, tmp_path: Path
) -> None:
    from dotbrave._base.cdp import CdpError

    configs_js = tmp_path / "configs.js"
    configs_js.write_text("// test\n", encoding="utf-8")
    prefs_path = profile_root / "Default" / "Preferences"

    plans = extensions.plan_apply_all(
        prefs_path,
        {},
        {
            "webstore": [
                {
                    "id": SURFINGKEYS_ID,
                    "messages": [
                        {
                            "action": "loadSettingsFromUrl",
                            "url": {"file_url": "configs.js"},
                        }
                    ],
                }
            ]
        },
        config_dir=tmp_path,
    )
    browser_plan = next(p for p in plans if p.external_apply_fn is None)

    fake_cdp = _FakeExtCdpClient(
        profile_root / "Default", installed_ids={SURFINGKEYS_ID}
    )
    orig_eval = fake_cdp.evaluate

    def failing_eval(target: dict, expr: str):
        if "chrome.runtime.sendMessage(" in expr:
            return {"status": "Failed", "error": "Failed to fetch"}
        return orig_eval(target, expr)

    fake_cdp.evaluate = failing_eval  # type: ignore[method-assign]
    target = fake_cdp.create_page("chrome://extensions")
    with pytest.raises(CdpError, match="Failed to fetch"):
        extensions.apply_via_cdp(fake_cdp, target, browser_plan)
