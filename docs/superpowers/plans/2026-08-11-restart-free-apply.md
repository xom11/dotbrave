# Restart-Free Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the cases where `dotbrave apply` has to close and relaunch Brave down to the ones Chromium genuinely forces, and fix the correctness bugs on the fallback path it leans on.

**Architecture:** No new runtime components. One LaunchAgent plist gives Brave a debugging endpoint from login, so the first apply of a session stops paying for a relaunch. Everything else is surgery inside the existing live adapter (`src/dotbrave/live.py`), the shared engine (`src/dotbrave/_base/`), and the settings sidecar: per-key rather than all-or-nothing fallback, live removals driven by a recorded prior value, and typed CDP errors so a live failure degrades instead of aborting.

**Tech Stack:** Python 3.11+, stdlib only. pytest. Chrome DevTools Protocol over `--remote-debugging-port` (HTTP + WebSocket). macOS `launchd` for the plist.

Spec: `docs/superpowers/specs/2026-08-11-restart-free-apply-design.md`

## Global Constraints

- Python 3.11+, **stdlib only**. No new dependencies, in `src/` or in tests.
- Shared engine code lives in `src/dotbrave/_base/`; Brave-specific behaviour lives in the top-level modules. Do not move behaviour across that line.
- `_base/` mirrors upstream `xom11/dotbrowser`. Keep changes there generic — no `brave` string literals, no Brave-only assumptions.
- The CLI surface stays exactly two actions, `apply` and `export`. New capability is a flag on one of them, never a new action (invariant 6).
- Policy paths, privilege writers, and process callbacks stay patchable for tests.
- Runtime `--help` is part of the capability contract (invariant 9). Any behaviour change that contradicts help text updates the help text in the same task.
- Endpoints bind to `127.0.0.1` and stay internal. Do not expose a public endpoint or a force-kill switch.
- Run the suite with `pytest -q` from the repo root. Run the CLI as `PYTHONPATH=src python -m dotbrave`.
- Commit after every task. No `Co-Authored-By` trailers.
- Work on branch `restart-free-apply` (already created; the spec commit is `39c80f3`).

## Out of scope

Phase 3 of the spec — the pipe-owning lifecycle agent for `[pwa]` — is **not** in this plan. It is gated on the experiment in the spec and gets its own spec and plan if that experiment passes.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `contrib/org.dotbrave.brave-endpoint.plist` | *(new)* macOS LaunchAgent that starts Brave at login with a dynamic debugging port | 1 |
| `tests/test_launch_agent_plist.py` | *(new)* validates the shipped plist parses and carries the right arguments | 1 |
| `README.md` | install/uninstall instructions for the plist | 1 |
| `src/dotbrave/_base/orchestrator.py` | re-read Preferences after a close; split-run messaging; not-open-profile shortcut | 2, 8, 11 |
| `src/dotbrave/_base/process.py` | `_LIVE_OWNED_FLAGS` gains the pipe flag | 3 |
| `src/dotbrave/_base/pwa.py` | drop the stale `dotbrave pwa dump` reference | 4 |
| `src/dotbrave/settings.py` | `LOCAL_STATE_KEYS` denylist (Brave-specific) | 5 |
| `src/dotbrave/_base/settings.py` | refuse Local State keys; sidecar prior-value schema | 5, 9 |
| `src/dotbrave/_base/cdp.py` | `CdpError` replaces `sys.exit` | 6 |
| `src/dotbrave/live.py` | readiness guard, shortcuts preflight, per-key split, live removals, profile-targeted work tab | 6, 7, 8, 10, 12 |
| `src/dotbrave/_base/live_apply.py` | removals returned rather than refused; prior-value merge helper | 8, 9 |
| `CLAUDE.md` | invariant amendments 1, 2, 5, 9 | 2, 8, 9 |

---

### Task 1: Endpoint LaunchAgent plist

Removes the most frequent restart: a Brave started from the Dock has no debugging endpoint, so `orchestrator.py:459-500` closes and relaunches it once per browser session before it can apply anything live. A dynamic port (`=0`) is required — Brave only writes `DevToolsActivePort` for a dynamic port, and that file is what `find_devtools_port` reads.

**Files:**
- Create: `contrib/org.dotbrave.brave-endpoint.plist`
- Create: `tests/test_launch_agent_plist.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing importable. Later tasks assume a Brave that *may* already have an endpoint; none depend on this file.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_launch_agent_plist.py
"""The shipped LaunchAgent must give Brave a *dynamic* debugging port.

Brave writes DevToolsActivePort only for --remote-debugging-port=0; started
on a fixed port it writes nothing and dotbrave's file-based discovery sees
no endpoint, which is the exact restart this plist exists to remove.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parents[1] / "contrib" / "org.dotbrave.brave-endpoint.plist"


def _load() -> dict:
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_parses_and_is_labelled() -> None:
    assert PLIST.exists(), f"missing {PLIST}"
    data = _load()
    assert data["Label"] == "org.dotbrave.brave-endpoint"


def test_plist_starts_brave_with_a_dynamic_debugging_port() -> None:
    args = _load()["ProgramArguments"]
    assert args[0].endswith("/Contents/MacOS/Brave Browser")
    assert "--remote-debugging-port=0" in args
    assert not any(
        a.startswith("--remote-debugging-port=") and a != "--remote-debugging-port=0"
        for a in args
    )
    assert "--remote-debugging-pipe" not in args


def test_plist_runs_at_login_and_does_not_resurrect_a_quit_browser() -> None:
    data = _load()
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_launch_agent_plist.py -q`
Expected: FAIL — `missing .../contrib/org.dotbrave.brave-endpoint.plist`

- [ ] **Step 3: Create the plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>org.dotbrave.brave-endpoint</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Brave Browser.app/Contents/MacOS/Brave Browser</string>
    <string>--remote-debugging-port=0</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_launch_agent_plist.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Document install and the two footguns in README.md**

Add a section under the macOS notes. Both warnings are load-bearing: macOS refuses a second instance of the same bundle, so a Brave already launched as a login item wins and the agent's launch dies on signal 9; and this makes Brave start at login, which is a real behaviour change.

````markdown
### macOS: keep a live endpoint from login (optional)

Without a debugging endpoint, the first `dotbrave apply` of each browser
session has to close Brave and relaunch it once just to obtain one. A user
LaunchAgent removes that:

```bash
cp contrib/org.dotbrave.brave-endpoint.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/org.dotbrave.brave-endpoint.plist
```

To remove it:

```bash
launchctl bootout gui/$UID/org.dotbrave.brave-endpoint
rm ~/Library/LaunchAgents/org.dotbrave.brave-endpoint.plist
```

Two things to know before installing:

- **Remove Brave from System Settings → General → Login Items.** macOS does
  not allow a second instance of the same app bundle, so whichever launches
  first wins and the other exits immediately. If Brave is already a login
  item, the agent's launch is the one that dies, and you get no endpoint.
- **This starts Brave at login.** The agent does not restart Brave if you
  quit it; a Brave you reopen yourself has no endpoint, and apply falls back
  to closing it once, as before.

The port is dynamic (`=0`) on purpose: Brave writes `DevToolsActivePort`
only for a dynamic port, and that file is how dotbrave finds the endpoint.
````

- [ ] **Step 6: Commit**

```bash
git add contrib/org.dotbrave.brave-endpoint.plist tests/test_launch_agent_plist.py README.md
git commit -m "feat(macos): ship a LaunchAgent that gives Brave a live endpoint from login"
```

---

### Task 2: Re-read Preferences after the graceful close (B1, data loss)

`prefs` is read once at `orchestrator.py:304`. The offline fallback then closes Brave — which flushes Brave's in-memory PrefService over the file — and writes the **pre-close** dict at `:575`. Everything Brave wrote at shutdown is reverted, including `profile.exit_type`, so the next launch sees an unclean-shutdown marker.

**Files:**
- Modify: `src/dotbrave/_base/orchestrator.py` (insert before the backup block at `:552`)
- Modify: `tests/test_live_apply.py`
- Modify: `CLAUDE.md` (invariant 1)

**Interfaces:**
- Consumes: nothing.
- Produces: after this task, any code running after `graceful_close_fn()` sees a freshly loaded `prefs` dict. Task 8 relies on this.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_live_apply.py`. It uses the `_args` / `_profile` helpers already in that file.

```python
def test_offline_fallback_does_not_discard_what_the_browser_flushed_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing Brave flushes its own Preferences copy; the offline write
    must build on that file, not on the snapshot read before the close."""
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    prefs_path = profile_root / "Default" / "Preferences"

    def flush_on_close() -> None:
        # What Brave writes as it exits: a key dotbrave never saw.
        data = json.loads(prefs_path.read_text())
        data["profile"] = {"exit_type": "Normal"}
        prefs_path.write_text(json.dumps(data))

    monkeypatch.setattr(orch, "find_devtools_port", lambda _root, _profile: 9444)

    def live_apply_fn(port, got_prefs_path, _prefs, plans):
        raise live_apply.LiveApplyUnsupported("Brave", ["foo.bar"])

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=live_apply_fn,
        graceful_close_fn=flush_on_close,
        launch_live_fn=lambda root, profile, port, url, captured=None: ["brave"],
    )

    final = json.loads(prefs_path.read_text())
    assert final["profile"]["exit_type"] == "Normal", "close-time flush was reverted"
    assert final["foo"]["bar"] == 1, "the requested change was not applied"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_live_apply.py::test_offline_fallback_does_not_discard_what_the_browser_flushed_on_close -q`
Expected: FAIL — `KeyError: 'profile'` (the pre-close snapshot has no `profile` key, and it overwrote the file)

- [ ] **Step 3: Re-read prefs after the close**

In `src/dotbrave/_base/orchestrator.py`, immediately before the backup block that begins `backup = prefs_path.with_suffix(` (currently line 552), insert:

```python
    if was_closed:
        # The close flushed the browser's own PrefService over this file.
        # Everything below mutates and rewrites `prefs`, so it has to be
        # the post-close copy or the write reverts the flush -- including
        # `profile.exit_type`, which then reads as an unclean shutdown.
        # Plans stay valid: their apply_fn closures carry the target from
        # the config and the removals from the sidecar, not a prefs
        # snapshot.
        prefs = load_prefs(prefs_path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_live_apply.py -q`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Run the whole suite**

Run: `pytest -q`
Expected: PASS. If a test asserted the old (stale-write) behaviour, it was asserting a bug — update it and say so in the commit body.

- [ ] **Step 6: Amend invariant 1 in CLAUDE.md**

In the invariant 1 paragraph, after the existing sentence about one backup per offline apply, add:

```markdown
   When an apply closes the browser, it must re-read `Preferences` before
   mutating and committing: the close flushes the browser's own in-memory
   copy over the file, and writing the snapshot taken before the close
   reverts that flush (`profile.exit_type` among it).
```

- [ ] **Step 7: Commit**

```bash
git add src/dotbrave/_base/orchestrator.py tests/test_live_apply.py CLAUDE.md
git commit -m "fix(apply): re-read Preferences after closing the browser

The offline fallback read Preferences once, before the close, then wrote
that snapshot back afterwards -- discarding everything the browser flushed
as it exited, including profile.exit_type, so the next launch read as an
unclean shutdown."
```

---

### Task 3: Keep `--remote-debugging-pipe` out of relaunches

`_forwardable_flags` forwards every `-`-prefixed argument off a captured command line except the four in `_LIVE_OWNED_FLAGS`. The pipe flag is not among them. A browser started with a pipe that gets relaunched by dotbrave is respawned with `stdin=DEVNULL` and no fd 3/4 — it reads EOF and quits within ~2s, then `_reopen_after_failed_relaunch` retries with the same flag and it dies again. This lands ahead of any agent work because nothing else protects against it.

**Files:**
- Modify: `src/dotbrave/_base/process.py:660-665`
- Modify: `tests/test_live_relaunch.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BrowserProcess._LIVE_OWNED_FLAGS` gains a fifth entry, `"--remote-debugging-pipe"`.

- [ ] **Step 1: Write the failing test**

```python
def test_relaunch_drops_a_pipe_flag_it_cannot_honour() -> None:
    """A relaunch spawns with stdin=DEVNULL and no fd 3/4, so a forwarded
    --remote-debugging-pipe makes the new browser read EOF and quit."""
    from dotbrave._base.process import BrowserProcess

    proc = BrowserProcess(name="brave", display_name="Brave")
    captured = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "--remote-debugging-pipe",
        "--ozone-platform=wayland",
        "--user-data-dir=/somewhere",
    ]

    flags = proc._forwardable_flags(captured)

    assert "--remote-debugging-pipe" not in flags
    assert "--ozone-platform=wayland" in flags, "session flags must still ride along"
```

Note: construct `BrowserProcess` the way the existing tests in this file do; if its constructor takes different arguments, match them rather than the sketch above.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_live_relaunch.py::test_relaunch_drops_a_pipe_flag_it_cannot_honour -q`
Expected: FAIL — `assert '--remote-debugging-pipe' not in flags`

- [ ] **Step 3: Add the flag to the owned list**

```python
    _LIVE_OWNED_FLAGS = (
        "--user-data-dir",
        "--profile-directory",
        "--remote-debugging-address",
        "--remote-debugging-port",
        # A relaunch spawns detached with stdin=DEVNULL and no fd 3/4, so a
        # forwarded pipe flag makes the new process read EOF and quit in
        # ~2s -- and the reopen retries the same command line.
        "--remote-debugging-pipe",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_live_relaunch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/_base/process.py tests/test_live_relaunch.py
git commit -m "fix(live): never forward --remote-debugging-pipe into a relaunch"
```

---

### Task 4: Drop the stale `dotbrave pwa dump` reference (B5)

`_base/pwa.py:566` emits `# Generated by \`dotbrave pwa dump\``. That subcommand does not exist — `browser.py:312` passes `module_registers=[]`. Invariant 9 forbids advertising removed actions.

**Files:**
- Modify: `src/dotbrave/_base/pwa.py:566`
- Modify: `tests/test_pwa_apply.py`

**Interfaces:**
- Consumes: nothing. Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
def test_generated_header_does_not_advertise_a_removed_subcommand() -> None:
    """dotbrave registers only `apply` and `export` (browser.py passes
    module_registers=[]), so no generated text may name `pwa dump`."""
    import subprocess, sys, os
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    hits = subprocess.run(
        ["grep", "-rn", "pwa dump", str(src), "--include=*.py"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert hits == "", f"removed subcommand still advertised:\n{hits}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_pwa_apply.py::test_generated_header_does_not_advertise_a_removed_subcommand -q`
Expected: FAIL, naming `_base/pwa.py:566`

- [ ] **Step 3: Fix the header**

In `src/dotbrave/_base/pwa.py`, change the header line to name the action that exists:

```python
    header = "# Generated by `dotbrave export`"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_pwa_apply.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dotbrave/_base/pwa.py tests/test_pwa_apply.py
git commit -m "fix(export): stop advertising the removed `pwa dump` action"
```

---

### Task 5: Refuse `[settings]` keys that live in Local State (B3)

`[settings]` addresses `<root>/<profile>/Preferences` only, but several user-facing prefs live in the browser-wide `Local State`. dotbrave writes them into the profile file and `verify_fn` re-reads *the file dotbrave just wrote*, so it confirms its own write and prints `ok -- applied and verified` for a pref the browser will never read. Refuse them with a message that says where they actually live.

The list is curated and exact, not prefixed: `browser.show_home_button` is a profile pref, so a `browser.` prefix would be wrong.

**Files:**
- Modify: `src/dotbrave/settings.py` (add `LOCAL_STATE_KEYS`)
- Modify: `src/dotbrave/_base/settings.py` (`plan_apply` refusal)
- Modify: `tests/test_settings_apply.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `dotbrave.settings.LOCAL_STATE_KEYS: frozenset[str]`, and `_base.settings.plan_apply` gains a keyword-only parameter `local_state_keys: frozenset[str] = frozenset()`.

- [ ] **Step 1: Write the failing test**

```python
def test_local_state_keys_are_refused_instead_of_silently_no_op(
    fake_settings_profile_root: Path, capsys
) -> None:
    """These prefs live in <root>/Local State, not <root>/<profile>/Preferences.
    Writing them to Preferences is a permanent no-op that verify_fn cannot
    catch, because verify_fn re-reads the file dotbrave itself wrote."""
    import pytest
    from dotbrave._base import settings as base_settings
    from dotbrave.settings import LOCAL_STATE_KEYS

    prefs_path = fake_settings_profile_root / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text())

    with pytest.raises(SystemExit) as excinfo:
        base_settings.plan_apply(
            "brave",
            prefs_path,
            prefs,
            {"browser.enabled_labs_experiments": ["some-flag@1"]},
            local_state_keys=LOCAL_STATE_KEYS,
        )

    message = str(excinfo.value)
    assert "browser.enabled_labs_experiments" in message
    assert "Local State" in message


def test_profile_scoped_browser_key_is_still_accepted(
    fake_settings_profile_root: Path
) -> None:
    """The denylist is exact, not prefixed: browser.* is not all Local State."""
    from dotbrave.settings import LOCAL_STATE_KEYS

    assert "browser.enabled_labs_experiments" in LOCAL_STATE_KEYS
    assert "browser.show_home_button" not in LOCAL_STATE_KEYS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_settings_apply.py -q -k local_state`
Expected: FAIL — `ImportError: cannot import name 'LOCAL_STATE_KEYS'`

- [ ] **Step 3: Add the curated list**

In `src/dotbrave/settings.py`, next to `KNOWN_SETTINGS`:

```python
# Prefs that live in `<profile-root>/Local State`, not in a profile's
# `Preferences`.  dotbrave only addresses the latter, so writing these
# there is a permanent no-op that `verify_fn` cannot catch -- it re-reads
# the file dotbrave just wrote.  Exact keys, never prefixes:
# `browser.show_home_button` is a profile pref.
LOCAL_STATE_KEYS: frozenset[str] = frozenset({
    "browser.enabled_labs_experiments",
    "performance_tuning.high_efficiency_mode",
    "hardware_acceleration_mode_previous",
})
```

- [ ] **Step 4: Refuse them in `plan_apply`**

In `src/dotbrave/_base/settings.py`, change the signature and extend the existing rejection loop (which already collects `rejected` and exits):

```python
def plan_apply(
    browser_name: str,
    prefs_path: Path,
    prefs: dict,
    raw_table: object,
    *,
    local_state_keys: frozenset[str] = frozenset(),
) -> Plan:
    target = _validate_table(raw_table)

    macs = _all_macs(prefs, prefs_path)
    rejected: list[str] = []
    for key in target:
        parts = _split_key(key)
        if key in local_state_keys:
            rejected.append(
                f"{key} (lives in Local State, not this profile's Preferences; "
                f"dotbrave cannot write it)"
            )
            continue
        if parts[0] == "protection":
```

Leave the rest of the loop and the `sys.exit` untouched.

- [ ] **Step 5: Pass the list from the Brave wrapper**

In `src/dotbrave/settings.py`, wherever the module forwards to `_base.settings.plan_apply`, pass `local_state_keys=LOCAL_STATE_KEYS`. Read the surrounding wrapper first — this module keeps module-level state patchable for tests, so match the existing forwarding style rather than inventing one.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_settings_apply.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/dotbrave/settings.py src/dotbrave/_base/settings.py tests/test_settings_apply.py
git commit -m "fix(settings): refuse keys that live in Local State

Written to a profile's Preferences they are a permanent no-op, and
verify_fn cannot catch it because it re-reads the file dotbrave wrote."
```

---

### Task 6: Typed CDP errors instead of `sys.exit`

`_base/cdp.py` calls `sys.exit` on any CDP error, JS exception, or unreachable endpoint. `orchestrator.py:504` catches only `LiveApplyUnsupported`, so those aborts skip the fallback entirely — after a backup has been taken and, in a mixed run, after `[pwa]` policy has already been written. This is the foundation Tasks 7 and 8 build on.

**Files:**
- Modify: `src/dotbrave/_base/cdp.py`
- Modify: `src/dotbrave/live.py`
- Modify: `tests/test_cdp.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `dotbrave._base.cdp.CdpError(RuntimeError)`. `CdpClient` methods and `_WebSocket` raise it instead of calling `sys.exit`. `live.apply_live` catches it and re-raises `LiveApplyUnsupported`.

- [ ] **Step 1: Write the failing test**

```python
def test_cdp_failures_raise_instead_of_exiting() -> None:
    from dotbrave._base.cdp import CdpClient, CdpError
    import pytest

    client = CdpClient(1)  # nothing listening on port 1
    with pytest.raises(CdpError):
        client.list_targets()


def test_live_apply_translates_cdp_errors_into_a_recoverable_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """A CDP failure must degrade to the offline path, not abort the run."""
    import json, pytest
    from dotbrave._base import cdp as cdp_mod
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"foo": {"bar": 0}}
    prefs_path.write_text(json.dumps(prefs))

    class ExplodingClient:
        def __init__(self, port): pass
        def list_targets(self): raise cdp_mod.CdpError("boom")
        def create_page(self, url="about:blank"): raise cdp_mod.CdpError("boom")

    monkeypatch.setattr(live, "CdpClient", ExplodingClient)

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=lambda target: target["foo"].__setitem__("bar", 1),
        verify_fn=lambda _p: None,
    )

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_cdp.py -q -k "raise or translate"`
Expected: FAIL — `ImportError: cannot import name 'CdpError'`

- [ ] **Step 3: Add the exception and replace every `sys.exit` in `cdp.py`**

```python
class CdpError(RuntimeError):
    """A DevTools request failed.

    Raised rather than exiting so a live adapter can fall back to an
    offline apply.  Every `sys.exit` this replaced aborted the run after
    a backup -- and possibly an external policy write -- had happened.
    """
```

Replace each `sys.exit("error: …")` in `_WebSocket._handshake`, `_WebSocket.recv_text`, `_WebSocket._read_exact`, `CdpClient._json`, `CdpClient.evaluate`, and `CdpClient._command` with `raise CdpError(…)`, dropping the `error: ` prefix from the message (the caller adds context now).

- [ ] **Step 4: Fix the one caller that depended on `SystemExit`**

`wait_for_devtools_endpoint` catches `SystemExit` from `CdpClient(port).list_targets()`. Change that handler:

```python
        try:
            targets = CdpClient(port).list_targets()
            if targets:
                return
        except CdpError as e:
            last_error = str(e)
```

Its own terminal `sys.exit` stays: it is the orchestrator's signal for a relaunch that never came up, and `orchestrator.py:493` already catches `SystemExit` there.

- [ ] **Step 5: Translate at the adapter boundary**

In `src/dotbrave/live.py`, import `CdpError` and wrap the body of `apply_live`'s `try` so a CDP failure becomes a recoverable fallback:

```python
    except CdpError as e:
        # Degrade to the offline path rather than aborting: a backup has
        # been taken by now, and in a mixed run [pwa] policy is written.
        raise _live.LiveApplyUnsupported("Brave", [f"live apply failed: {e}"])
```

Place it as an `except` on the existing `try`, before the `finally` that closes the work tab.

- [ ] **Step 6: Run the suite**

Run: `pytest -q`
Expected: PASS. Tests that asserted `SystemExit` out of CDP paths now assert `CdpError`; update them.

- [ ] **Step 7: Commit**

```bash
git add src/dotbrave/_base/cdp.py src/dotbrave/live.py tests/
git commit -m "fix(live): raise CdpError instead of exiting, so live failures fall back

A CDP error aborted the run outright -- after the backup, and after any
[pwa] policy write -- because the orchestrator only catches
LiveApplyUnsupported."
```

---

### Task 7: Page-readiness guard and a `[shortcuts]` preflight

All three live routes navigate then `time.sleep(0.5)`, but `Page.navigate` returns as soon as navigation *starts*. The settings preflight then dereferences `chrome.settingsPrivate` unguarded (`live.py:190-194`), as does the mutating script (`live.py:130-131`), so a slow load turns a fully live-capable change into a failure. `[shortcuts]` has no preflight at all, so a renamed bundle export fails the same way.

**Files:**
- Modify: `src/dotbrave/live.py`
- Modify: `tests/test_brave_live.py`

**Interfaces:**
- Consumes: `CdpError` from Task 6.
- Produces: `live._shortcuts_preflight_script() -> str`, returning `[]` when the commands bundle is usable and `["shortcuts"]` when it is not.

- [ ] **Step 1: Write the failing test**

```python
def test_shortcuts_preflight_reports_unsupported_instead_of_failing_hard(
    tmp_path: Path, monkeypatch
) -> None:
    """A renamed commands bundle must degrade to the offline path."""
    import pytest
    from dotbrave.command_ids import NAME_TO_ID
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    new_tab = str(NAME_TO_ID["new_tab"])
    prefs = {"brave": {"accelerators": {new_tab: ["Control+KeyT"]},
                       "default_accelerators": {new_tab: ["Control+KeyT"]}}}
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["accelerators"][new_tab] = ["Control+Shift+KeyY"]

    plan = Plan(namespace="shortcuts", diff_lines=["changed"],
                apply_fn=apply_fn, verify_fn=lambda _p: None)

    # settings preflight returns [], shortcuts preflight reports itself broken
    fake = FakeCdpClient(9333, evaluation_results=[["shortcuts"]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])
    assert any("shortcut" in k for k in excinfo.value.keys)


def test_scripts_guard_against_an_unloaded_settings_page() -> None:
    """The probe must degrade like the NTP one, not throw on a cold page."""
    from dotbrave import live

    script = live._settings_preflight_script([("foo.bar", 1)])
    assert "chrome?.settingsPrivate" in script or "typeof chrome" in script
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_brave_live.py -q -k "shortcuts_preflight or unloaded"`
Expected: FAIL — no shortcuts preflight exists; the settings probe has no guard.

- [ ] **Step 3: Guard the settings scripts**

In `_settings_preflight_script`, replace the unguarded call with a guarded one:

```python
        "const api = (typeof chrome !== 'undefined') && chrome.settingsPrivate;"
        "if (!api) return keys.slice();"
        "const exists = key => new Promise(resolve => {"
        "api.getPref(key, pref => {"
```

Apply the same guard in `_settings_script`, returning a rejected promise naming the keys so the caller can treat them as unsupported rather than crashing.

- [ ] **Step 4: Add the shortcuts preflight**

```python
def _shortcuts_preflight_script() -> str:
    """Probe the commands bundle the way the NTP probe works: report
    unusable rather than throwing, so a renamed export degrades to an
    offline apply instead of aborting the run."""
    return (
        "(async () => {"
        "try {"
        "const m = await import('/commands.bundle.js');"
        "const c = m.commandsCache;"
        "if (!c || typeof c.assignAccelerator !== 'function' "
        "|| typeof c.unassignAccelerator !== 'function') return ['shortcuts'];"
        "return [];"
        "} catch (e) { return ['shortcuts']; }"
        "})()"
    )
```

Call it in `apply_live` before running the shortcut script, only when `_shortcut_script(...)` returned something, and merge its result into the same unsupported list the settings preflight feeds.

- [ ] **Step 5: Wait for the page instead of sleeping blind**

Add a helper and use it after each `client.navigate(...)` in place of `time.sleep(0.5)`:

```python
def _await_page(client: CdpClient, target: dict, timeout: float = 10.0) -> None:
    """Poll until the page has a live JS context.

    ``Page.navigate`` returns when navigation *starts*; the old fixed
    0.5s sleep turned a cold profile or a slow machine into a failed
    live apply.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.evaluate(target, "document.readyState === 'complete'") is True:
            return
        time.sleep(0.1)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_brave_live.py -q`
Expected: PASS. `FakeCdpClient.evaluate` returns `[]` once its scripted results run out, so `_await_page` will spin to its timeout in tests that do not script a `True`. Give the fake a readiness result, or have `_await_page` accept a falsy result as "not ready yet" and cap the loop at a short timeout in tests via monkeypatch — pick one and apply it consistently across the file.

- [ ] **Step 7: Commit**

```bash
git add src/dotbrave/live.py tests/test_brave_live.py
git commit -m "fix(live): guard privileged-page scripts and preflight [shortcuts]

Page.navigate returns before the page has a JS context, so the fixed
0.5s sleep plus an unguarded chrome.settingsPrivate dereference turned a
slow load into a failed apply. [shortcuts] had no preflight at all."
```

---

### Task 8: Per-key split apply

Today one unsupported key sends the whole run offline — including every key that would have worked and the entire `[shortcuts]` table. Apply what can be applied live, then fall back for the remainder only.

**Files:**
- Modify: `src/dotbrave/_base/live_apply.py`
- Modify: `src/dotbrave/live.py`
- Modify: `src/dotbrave/_base/orchestrator.py` (messaging only)
- Modify: `tests/test_brave_live.py`, `tests/test_live_relaunch.py`
- Modify: `CLAUDE.md` (invariants 1 and 5)

**Interfaces:**
- Consumes: `CdpError` (Task 6), the preflight lists (Task 7).
- Produces: `_base.live_apply.split_removals(changes) -> tuple[list, list[str]]` replacing `refuse_live_removals`; `live._setting_changes` now returns `(changes, removal_keys)`.

- [ ] **Step 1: Write the failing test**

```python
def test_one_unsupported_key_no_longer_drags_the_whole_run_offline(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest
    from dotbrave.command_ids import NAME_TO_ID
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    new_tab = str(NAME_TO_ID["new_tab"])
    prefs = {
        "brave": {
            "tabs": {"vertical_tabs_collapsed": False},
            "location_bar_is_wide": False,
            "accelerators": {new_tab: ["Control+KeyT"]},
            "default_accelerators": {new_tab: ["Control+KeyT"]},
        }
    }
    prefs_path.write_text(json.dumps(prefs))

    def apply_fn(target: dict) -> None:
        target["brave"]["tabs"]["vertical_tabs_collapsed"] = True   # unsupported
        target["brave"]["location_bar_is_wide"] = True              # supported
        target["brave"]["accelerators"][new_tab] = ["Control+Shift+KeyY"]

    plan = Plan(namespace="settings", diff_lines=["changed"],
                apply_fn=apply_fn, verify_fn=lambda _p: None)

    # settings preflight reports only the vertical-tabs key as unsupported
    fake = FakeCdpClient(9333, evaluation_results=[["brave.tabs.vertical_tabs_collapsed"]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])

    # the remainder is named, and only the remainder
    assert excinfo.value.keys == ["brave.tabs.vertical_tabs_collapsed"]
    # ...but the supported key and the shortcut were applied live first
    assert any("brave.location_bar_is_wide" in e and "setPref" in e
               for e in fake.evaluations)
    assert any("assignAccelerator" in e for e in fake.evaluations)


def test_split_run_takes_no_live_backup(tmp_path: Path, monkeypatch) -> None:
    """The orchestrator backs up for the offline remainder; a second
    backup here would violate invariant 1."""
    ...  # same setup as above; assert no Preferences.bak.* next to prefs_path
```

Fill the second test's body with the same setup as the first, then:

```python
    backups = list(prefs_path.parent.glob("Preferences.bak.*"))
    assert backups == [], f"live path took a backup during a split run: {backups}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_brave_live.py -q -k split or drag`
Expected: FAIL — `apply_live` raises before applying anything.

- [ ] **Step 3: Return removals instead of refusing them**

In `src/dotbrave/_base/live_apply.py`, replace `refuse_live_removals` with:

```python
def split_removals(
    changes: list[tuple[tuple[str, ...], Any]],
) -> tuple[list[tuple[tuple[str, ...], Any]], list[str]]:
    """Separate applicable changes from deletions.

    Deleted leaves carry the ``MISSING`` sentinel.  ``settingsPrivate``
    has no single-pref reset, so they cannot be applied live -- but they
    are the *only* part of the run that has to go offline, so return them
    rather than refusing the whole batch.
    """
    applicable = [(parts, value) for parts, value in changes if value is not MISSING]
    removals = [".".join(parts) for parts, value in changes if value is MISSING]
    return applicable, removals
```

- [ ] **Step 4: Split the apply in `live.py`**

Change `_setting_changes` to return both halves:

```python
def _setting_changes(before: dict, target: dict) -> tuple[list[tuple[str, Any]], list[str]]:
    changes = [
        (parts, value)
        for parts, value in _live.changed_leaf_paths(before, target)
        if not _is_shortcut_path(parts)
    ]
    applicable, removals = _live.split_removals(changes)
    return [(".".join(parts), value) for parts, value in applicable], removals
```

Then in `apply_live`, after the preflight:

```python
        blocked = set(unsupported) | set(removals)
        live_newtab = [c for c in newtab_changes if c[0] not in blocked]
        live_ordinary = [c for c in ordinary_changes if c[0] not in blocked]
        remainder = sorted(blocked)

        has_pref_changes = any(
            not plan.empty and plan.namespace in {"settings", "shortcuts"}
            for plan in plans
        )
        # Only back up when this run finishes here.  A split run falls
        # through to the offline path, which takes its own backup, and
        # invariant 1 allows exactly one.
        if has_pref_changes and not remainder:
            _live.backup_preferences(prefs_path)
```

Run the newtab, settings and shortcut scripts against `live_newtab` / `live_ordinary` as before, then finish:

```python
        if remainder:
            # State files stay unwritten: the offline apply that handles
            # the remainder writes them for the whole plan.
            raise _live.LiveApplyUnsupported("Brave", remainder)

        _live.write_state_files(plans)
```

- [ ] **Step 5: Make the orchestrator's message honest about a split**

At `orchestrator.py:520-525`, the message claims the browser "cannot apply every requested setting live". That is now precisely true and should say what already landed:

```python
                print(
                    f"{display_name} applied everything it could live; "
                    f"closing it normally to finish these offline "
                    f"(no force-kill):"
                )
```

- [ ] **Step 6: Run the suite**

Run: `pytest -q`
Expected: PASS. `tests/test_brave_live.py:165-178` and `tests/test_live_relaunch.py:178` assert the old all-or-nothing behaviour — update them to assert a *named remainder* plus a live half.

- [ ] **Step 7: Amend CLAUDE.md invariants 1 and 5**

Invariant 1: after the existing backup sentence, add that a live apply takes a backup only when it completes without an offline remainder, so a split run still takes exactly one. Invariant 5: replace "Unsupported live settings and removals fall back to a normal close, verified offline apply, and relaunch" with a sentence saying only the unsupported keys and removals do, and that the rest is applied live first.

- [ ] **Step 8: Commit**

```bash
git add src/dotbrave/live.py src/dotbrave/_base/live_apply.py src/dotbrave/_base/orchestrator.py tests/ CLAUDE.md
git commit -m "feat(live): apply per key instead of all-or-nothing

One key settingsPrivate does not know sent the whole run offline,
including [shortcuts] and every key that would have worked. Apply the
live-capable half first and fall back only for the remainder."
```

---

### Task 9: Sidecar records the prior value of each managed key

Live removal needs a value to write, because `settingsPrivate` has no reset. Record, for each key at the moment dotbrave *first* manages it, what was there before. `absent` marks "the key was not set and we do not know the default" — those keys stay on the offline path.

**Files:**
- Modify: `src/dotbrave/_base/settings.py`
- Modify: `tests/test_settings_apply.py`
- Modify: `CLAUDE.md` (invariant 2)

**Interfaces:**
- Consumes: nothing.
- Produces: sidecar schema gains `prior_values: dict[str, dict]`, each entry `{"present": bool, "value": Any}`. `_base.settings.merge_prior_values(existing: dict, captured: dict) -> dict` keeps the first-seen entry. `_get_prior_values(prefs_path) -> dict` reads them back.

- [ ] **Step 1: Write the failing test**

```python
def test_sidecar_records_the_value_a_key_had_before_dotbrave_managed_it(
    fake_settings_profile_root: Path
) -> None:
    from dotbrave._base import settings as base_settings

    prefs_path = fake_settings_profile_root / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text())

    plan = base_settings.plan_apply(
        "brave", prefs_path, prefs,
        {"brave.tabs.vertical_tabs_enabled": True,   # present, False
         "brave.tabs.brand_new_key": True},          # absent
    )

    prior = plan.state_payload["prior_values"]
    assert prior["brave.tabs.vertical_tabs_enabled"] == {"present": True, "value": False}
    assert prior["brave.tabs.brand_new_key"] == {"present": False, "value": None}


def test_prior_values_are_first_seen_and_never_overwritten() -> None:
    from dotbrave._base.settings import merge_prior_values

    existing = {"a.b": {"present": True, "value": 1}}
    captured = {"a.b": {"present": True, "value": 999}, "c.d": {"present": False, "value": None}}

    merged = merge_prior_values(existing, captured)

    assert merged["a.b"] == {"present": True, "value": 1}, "first-seen must win"
    assert merged["c.d"] == {"present": False, "value": None}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_settings_apply.py -q -k prior`
Expected: FAIL — `KeyError: 'prior_values'` and `ImportError: merge_prior_values`

- [ ] **Step 3: Implement capture and merge**

In `src/dotbrave/_base/settings.py`:

```python
def merge_prior_values(existing: dict, captured: dict) -> dict:
    """First-seen wins.

    The recorded value is what a key had before dotbrave ever managed it,
    so a later apply must not overwrite it with dotbrave's own value --
    that would make removal restore dotbrave's setting, not the user's.
    """
    merged = dict(existing)
    for key, entry in captured.items():
        merged.setdefault(key, entry)
    return merged


def _get_prior_values(prefs_path: Path) -> dict:
    state = _state_file(prefs_path)
    if not state.exists():
        return {}
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    prior = data.get("prior_values")
    return prior if isinstance(prior, dict) else {}


def _capture_prior_values(prefs: dict, keys: set[str]) -> dict:
    captured = {}
    for key in keys:
        value = _get_value(prefs, _split_key(key))
        if value is _MISSING:
            captured[key] = {"present": False, "value": None}
        else:
            captured[key] = {"present": True, "value": value}
    return captured
```

In `plan_apply`, build the payload from both:

```python
    prior_values = merge_prior_values(
        _get_prior_values(prefs_path),
        _capture_prior_values(prefs, target_keys),
    )

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        state_path=_state_file(prefs_path),
        state_payload={
            "managed_keys": sorted(target_keys),
            "prior_values": prior_values,
        },
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        warnings=warnings,
    )
```

Check the sentinel name used by `_get_value` in this module (`_MISSING` at the top of the file) and match it exactly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_settings_apply.py -q`
Expected: PASS. `_get_managed_keys` reads `managed_keys` and ignores unknown fields, so old sidecars keep working; a sidecar without `prior_values` simply yields `{}`.

- [ ] **Step 5: Amend invariant 2 in CLAUDE.md**

State the route-dependent meaning: offline removal deletes the key; live removal writes back the recorded prior value, leaving the key present. A key recorded `present: false` cannot be removed live and goes to the offline remainder.

- [ ] **Step 6: Commit**

```bash
git add src/dotbrave/_base/settings.py tests/test_settings_apply.py CLAUDE.md
git commit -m "feat(settings): record each managed key's prior value in the sidecar

settingsPrivate has no single-pref reset, so a live removal needs a value
to write. First-seen wins, so removal restores the user's value rather
than dotbrave's."
```

---

### Task 10: Live removals

With a recorded prior value, a removal becomes `setPref(key, prior)` and stops forcing a close.

**Files:**
- Modify: `src/dotbrave/live.py`
- Modify: `tests/test_brave_live.py`

**Interfaces:**
- Consumes: `_base.settings._get_prior_values` (Task 9), the split from Task 8.
- Produces: nothing new; `apply_live`'s remainder shrinks to removals with no usable prior value.

- [ ] **Step 1: Write the failing test**

```python
def test_removal_with_a_recorded_prior_value_applies_live(
    tmp_path: Path, monkeypatch
) -> None:
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"location_bar_is_wide": True}}
    prefs_path.write_text(json.dumps(prefs))
    prefs_path.with_name("Preferences.dotbrave.settings.json").write_text(json.dumps({
        "managed_keys": ["brave.location_bar_is_wide"],
        "prior_values": {"brave.location_bar_is_wide": {"present": True, "value": False}},
    }))

    def apply_fn(target: dict) -> None:
        del target["brave"]["location_bar_is_wide"]   # dropped from config

    plan = Plan(namespace="settings", diff_lines=["removed"],
                apply_fn=apply_fn, verify_fn=lambda _p: None,
                state_path=prefs_path.with_name("Preferences.dotbrave.settings.json"),
                state_payload={"managed_keys": [], "prior_values": {}})

    fake = FakeCdpClient(9333, evaluation_results=[[]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    live.apply_live(9333, prefs_path, prefs, [plan])   # must NOT raise

    assert any("brave.location_bar_is_wide" in e and "false" in e and "setPref" in e
               for e in fake.evaluations)


def test_removal_without_a_usable_prior_value_still_goes_offline(
    tmp_path: Path, monkeypatch
) -> None:
    """present: false means we never learned the default, so there is
    nothing to write and the key has to be deleted offline."""
    import pytest
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"location_bar_is_wide": True}}
    prefs_path.write_text(json.dumps(prefs))
    prefs_path.with_name("Preferences.dotbrave.settings.json").write_text(json.dumps({
        "managed_keys": ["brave.location_bar_is_wide"],
        "prior_values": {"brave.location_bar_is_wide": {"present": False, "value": None}},
    }))

    def apply_fn(target: dict) -> None:
        del target["brave"]["location_bar_is_wide"]

    plan = Plan(namespace="settings", diff_lines=["removed"],
                apply_fn=apply_fn, verify_fn=lambda _p: None)

    fake = FakeCdpClient(9333, evaluation_results=[[]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    with pytest.raises(shared_live.LiveApplyUnsupported) as excinfo:
        live.apply_live(9333, prefs_path, prefs, [plan])
    assert excinfo.value.keys == ["brave.location_bar_is_wide"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_brave_live.py -q -k removal`
Expected: FAIL — both removals raise, because Task 8 routes every removal to the remainder.

- [ ] **Step 3: Resolve removals against the recorded prior values**

In `src/dotbrave/live.py`, after computing `removals`:

```python
def _resolve_removals(
    prefs_path: Path, removals: list[str],
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Turn removals into live writes where a prior value was recorded.

    ``settingsPrivate`` cannot delete a pref, but writing back what the
    key held before dotbrave managed it is the same thing as far as the
    browser's behaviour is concerned.  A key recorded ``present: false``
    was never set, so there is no value to write and it has to be deleted
    offline.
    """
    prior = _base_settings._get_prior_values(prefs_path)
    writes: list[tuple[str, Any]] = []
    unresolved: list[str] = []
    for key in removals:
        entry = prior.get(key)
        if isinstance(entry, dict) and entry.get("present") is True:
            writes.append((key, entry.get("value")))
        else:
            unresolved.append(key)
    return writes, unresolved
```

Import `from dotbrave._base import settings as _base_settings` at the top of `live.py`.

In `apply_live`, feed the resolved writes into the ordinary settings changes and let only `unresolved` reach `blocked`:

```python
        removal_writes, unresolved_removals = _resolve_removals(prefs_path, removals)
        ordinary_changes = ordinary_changes + removal_writes
        blocked = set(unsupported) | set(unresolved_removals)
```

Order matters: compute `removal_writes` *before* the preflight so the written-back keys are probed too — a prior value for a key `settingsPrivate` does not know is still unusable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_brave_live.py -q`
Expected: PASS

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dotbrave/live.py tests/test_brave_live.py
git commit -m "feat(live): apply [settings] removals live via the recorded prior value

Dropping a key from the config no longer forces a close, unless the key
was never set before dotbrave managed it and there is no value to restore."
```

---

### Task 11: Apply offline with no restart when the target profile is not open

`running_fn` is scoped to the user-data-dir, not the profile. A profile that is not open in the running browser has its `Preferences` untouched by Chromium, so it can be written offline right now — with a real `verify_fn` and no close at all.

**Files:**
- Modify: `src/dotbrave/_base/orchestrator.py`
- Modify: `src/dotbrave/browser.py` (supply the callback)
- Modify: `tests/test_profile_scope.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `cmd_apply` gains a keyword-only parameter `profile_open_fn: Callable[[], bool] | None = None`. When it returns `False`, the run skips the live path and every close, going straight to the offline block.

- [ ] **Step 1: Write the failing test**

```python
def test_apply_to_a_profile_that_is_not_open_never_closes_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = _profile(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[settings]\nfoo.bar = 1\n")
    closed: list[str] = []

    orch.cmd_apply(
        _args(profile_root, cfg),
        display_name="Brave",
        running_fn=lambda: True,          # a Brave IS running...
        profile_open_fn=lambda: False,    # ...but not on this profile
        find_cmdline_fn=lambda: ["brave"],
        restart_fn=lambda _cmd: [],
        build_plans_fn=_build_plan,
        live_apply_fn=lambda *a, **k: pytest.fail("live apply not needed"),
        graceful_close_fn=lambda: closed.append("closed"),
        launch_live_fn=lambda *a, **k: ["brave"],
    )

    assert closed == [], "closed a browser that does not hold this profile"
    prefs = json.loads((profile_root / "Default" / "Preferences").read_text())
    assert prefs["foo"]["bar"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_profile_scope.py -q -k not_open`
Expected: FAIL — `TypeError: cmd_apply() got an unexpected keyword argument 'profile_open_fn'`

- [ ] **Step 3: Add the parameter and the early branch**

Add `profile_open_fn: Callable[[], bool] | None = None` to `cmd_apply`'s signature. Where the running state is decided (around `orchestrator.py:399`), treat "running but this profile is not open" as not-running for the purposes of the browser-bound work:

```python
    if is_running and profile_open_fn is not None and not profile_open_fn():
        # The browser holds a different profile, so Chromium is not
        # touching this profile's Preferences.  Writing it offline is
        # safe, verifiable, and needs no close at all.
        print(
            f"{display_name} is running, but not on this profile -- "
            f"applying offline without closing it"
        )
        is_running = False
```

Match the actual local variable name at that point in the file rather than assuming `is_running`.

- [ ] **Step 4: Supply the callback from the Brave wrapper**

In `src/dotbrave/browser.py`'s `cmd_apply`, pass a `profile_open_fn` that reports whether the running browser has the target profile open. Derive it from the same command-line inspection `BrowserProcess.scope_to_profile` already performs: a process carrying `--profile-directory=<target>` (or, for the default profile, carrying no `--profile-directory` at all) holds it. If that cannot be determined, return `True` — the conservative answer keeps today's behaviour.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_profile_scope.py -q && pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dotbrave/_base/orchestrator.py src/dotbrave/browser.py tests/test_profile_scope.py
git commit -m "feat(apply): skip the close when the target profile is not open

A browser holding a different profile is not touching this profile's
Preferences, so the offline write is safe and needs no restart."
```

---

### Task 12: Verify, then fix, the work tab's profile (B4)

**This task starts with a measurement, because the bug is inferred, not observed.** `CdpClient.create_page` issues `PUT /json/new` with no profile hint, so the work tab is believed to land in the browser's last-used profile while the rest of the run is bound to `args.profile`. If that is true, `dotbrave -p "Profile 2" apply` can write live prefs into `Default` and then write Profile 2's sidecar, exiting 0.

**Files:**
- Modify: `src/dotbrave/live.py`
- Modify: `tests/test_profile_scope.py`

**Interfaces:**
- Consumes: `CdpError` (Task 6).
- Produces: `live._worker_target` gains a `profile` argument and raises `LiveApplyUnsupported` when it cannot confirm the tab belongs to that profile.

- [ ] **Step 1: Measure it on a real browser**

Requires the user's Brave to be **closed** — macOS refuses a second instance of the bundle. Use a throwaway root, and afterwards sweep `~/Applications/Brave Browser Apps.localized/` for any `.app` whose `Contents/Info.plist` `CrAppModeUserDataDir` points inside the throwaway root (the machine-wide managed PWA policy installs into every profile, whatever `--user-data-dir` says).

```bash
ROOT=$(mktemp -d)
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --user-data-dir="$ROOT" --remote-debugging-port=9333 \
  --no-first-run --no-default-browser-check --profile-directory=Default about:blank &
sleep 6
# create a second profile, open it, then create a work tab and see which
# profile's Preferences moves when settingsPrivate writes through it
curl -s -X PUT "http://127.0.0.1:9333/json/new?about:blank" | head -c 200
```

Record which `<profile>/Preferences` changes. If the work tab lands in the profile passed via `--profile-directory`, **stop here**: close the task with a note in the spec that B4 does not reproduce, and delete the remaining steps.

- [ ] **Step 2: Write the failing test (only if Step 1 reproduced the bug)**

```python
def test_live_apply_refuses_a_work_tab_from_the_wrong_profile(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Profile 2" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"brave": {"location_bar_is_wide": False}}
    prefs_path.write_text(json.dumps(prefs))

    class WrongProfileClient(FakeCdpClient):
        def create_page(self, url="about:blank"):
            target = super().create_page(url)
            target["profile"] = "Default"     # not the one we asked for
            return target

    fake = WrongProfileClient(9333, evaluation_results=[[]])
    monkeypatch.setattr(live, "CdpClient", lambda port: fake)

    plan = Plan(namespace="settings", diff_lines=["changed"],
                apply_fn=lambda t: t["brave"].__setitem__("location_bar_is_wide", True),
                verify_fn=lambda _p: None)

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan], profile="Profile 2")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_profile_scope.py -q -k wrong_profile`
Expected: FAIL — `apply_live()` takes no `profile` argument.

- [ ] **Step 4: Confirm the work tab's profile before using it**

Thread `profile` from `orchestrator`'s call into `apply_live`, and in `_worker_target` verify the created tab belongs to it — by evaluating a profile-identifying expression in the tab, or by matching the target's own metadata, whichever Step 1 showed to be reliable. When it cannot be confirmed, raise `LiveApplyUnsupported` naming the profile so the run falls back to a scoped offline apply rather than writing to the wrong profile.

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dotbrave/live.py tests/test_profile_scope.py
git commit -m "fix(live): confirm the work tab's profile before writing through it"
```

---

## Final verification

- [ ] `pytest -q` — full suite green
- [ ] `PYTHONPATH=src python -m dotbrave apply --help` — no promise of "closing it normally and relaunching once for live apply" that the new behaviour contradicts (invariant 9)
- [ ] `git log --oneline main..restart-free-apply` — one commit per task, spec commit at the base
- [ ] Re-read `CLAUDE.md` invariants 1, 2, 5, 9 against the shipped behaviour

## Self-review notes

Checked against the spec:

- Spec §Phase 2 → Task 1. §Phase 1.1 → Task 8. §1.2 → Tasks 9–10. §1.3 → Task 6. §1.4 and §1.5 → Task 7. §1.6 → Task 12. §1.7 → Task 11. §1.8 → Tasks 2–5 plus the flag change in Task 3.
- B1 → Task 2, B2 → folded into Task 8 Step 4 (it is not a bug today; the split run would create it), B3 → Task 5, B4 → Task 12, B5 → Task 4.
- Not covered here by design: Phase 3 and its gating experiment; the `getPref` sweep over `examples/all.toml`, which needs a real browser with the user's Brave closed and belongs with that experiment.
- Interface names used across tasks and defined once: `CdpError` (6), `split_removals` (8), `merge_prior_values` / `_get_prior_values` (9), `_resolve_removals` (10), `profile_open_fn` (11).
- Task 12 is deliberately conditional; its Step 1 can close the task with no code change.
