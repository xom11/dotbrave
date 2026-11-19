from __future__ import annotations

import json
from pathlib import Path

from dotbrave._base import cdp


def test_remember_devtools_port_records_profile_and_port(tmp_path: Path) -> None:
    cdp.remember_devtools_port(tmp_path, "Profile 1", 9444)

    data = json.loads((tmp_path / ".dotbrave.live.json").read_text())

    assert data == {"profile": "Profile 1", "port": 9444}


def test_find_devtools_port_reads_dotbrave_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Default", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: port == 9444)

    assert cdp.find_devtools_port(tmp_path, "Default") == 9444


def test_find_devtools_port_ignores_other_profile_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Profile 1", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda _port: True)

    assert cdp.find_devtools_port(tmp_path, "Default") is None


def test_find_devtools_port_ignores_stale_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Default", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda _port: False)

    assert cdp.find_devtools_port(tmp_path, "Default") is None


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


class _MockWsOnlyDevToolsServer:
    """Loopback server mimicking ``brave://inspect/#remote-debugging``:
    HTTP ``/json/*`` routes return 404 while ``/devtools/browser/<uuid>``
    speaks Browser-level CDP over RFC 6455 WebSocket."""

    def __init__(self, profile_dir: Path, ws_path: str = "/devtools/browser/test-uuid") -> None:
        import socket
        import threading

        self.profile_dir = profile_dir
        self.ws_path = ws_path
        self.commands: list[dict] = []
        self.http_paths: list[str] = []
        self._current_url = "about:blank"
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.port = int(self._srv.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_MockWsOnlyDevToolsServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        import socket

        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                self._handle_conn(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_conn(self, conn) -> None:
        import base64
        import hashlib
        import struct

        conn.settimeout(2.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        header_text = buf.decode("iso-8859-1", "replace")
        lines = header_text.split("\r\n")
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket" or path != self.ws_path:
            self.http_paths.append(path)
            conn.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return

        key = headers["sec-websocket-key"]
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

        def send_ws_json(obj: dict) -> None:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            hdr = bytearray([0x81])
            n = len(raw)
            if n < 126:
                hdr.append(n)
            elif n <= 0xFFFF:
                hdr.append(126)
                hdr.extend(struct.pack("!H", n))
            else:
                hdr.append(127)
                hdr.extend(struct.pack("!Q", n))
            conn.sendall(bytes(hdr) + raw)

        def read_exact(n: int) -> bytes | None:
            out = bytearray()
            while len(out) < n:
                ch = conn.recv(n - len(out))
                if not ch:
                    return None
                out.extend(ch)
            return bytes(out)

        while not self._stop.is_set():
            first = read_exact(2)
            if first is None:
                return
            opcode = first[0] & 0x0F
            if opcode == 0x8:
                return
            length = first[1] & 0x7F
            masked = bool(first[1] & 0x80)
            if length == 126:
                ext = read_exact(2)
                if ext is None:
                    return
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = read_exact(8)
                if ext is None:
                    return
                length = struct.unpack("!Q", ext)[0]
            mask = read_exact(4) if masked else b""
            if mask is None:
                return
            payload = read_exact(length)
            if payload is None:
                return
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            msg = json.loads(payload.decode("utf-8"))
            self.commands.append(msg)
            req_id = msg["id"]
            method = msg["method"]
            params = msg.get("params") or {}

            # Emit an unsolicited event first to ensure CdpClient skips events.
            send_ws_json({"method": "Target.targetInfoChanged", "params": {}})

            if method == "Target.getTargets":
                send_ws_json(
                    {
                        "id": req_id,
                        "result": {
                            "targetInfos": [
                                {
                                    "targetId": "existing-tab",
                                    "type": "page",
                                    "url": "https://example.com/",
                                }
                            ]
                        },
                    }
                )
            elif method == "Target.createTarget":
                send_ws_json({"id": req_id, "result": {"targetId": "work-tab-1"}})
            elif method == "Target.attachToTarget":
                send_ws_json({"id": req_id, "result": {"sessionId": "sess-1"}})
            elif method == "Target.closeTarget":
                send_ws_json({"id": req_id, "result": {"success": True}})
            elif method == "Page.navigate":
                self._current_url = params.get("url", "")
                send_ws_json(
                    {
                        "id": req_id,
                        "sessionId": msg.get("sessionId"),
                        "result": {"frameId": "f1"},
                    }
                )
            elif method == "Runtime.evaluate":
                expr = params.get("expression", "")
                if "readyState" in expr:
                    val = True
                elif "profile_path" in expr:
                    val = str(self.profile_dir)
                elif "getPref" in expr:
                    val = [
                        {
                            "key": "brave.tabs.vertical_tabs_enabled",
                            "supported": True,
                            "value": False,
                        }
                    ]
                else:
                    val = True
                send_ws_json(
                    {
                        "id": req_id,
                        "sessionId": msg.get("sessionId"),
                        "result": {"result": {"type": typeof_val(val), "value": val}},
                    }
                )


def typeof_val(val) -> str:
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, str):
        return "string"
    return "object"


def test_ws_only_devtools_discovery_and_live_apply(tmp_path: Path) -> None:
    from dotbrave import live
    from dotbrave._base.utils import Plan

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    prefs_path = profile_dir / "Preferences"
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    with _MockWsOnlyDevToolsServer(profile_dir) as srv:
        # Without line 2 (/devtools/browser/...), a 404 port is ignored.
        (tmp_path / "DevToolsActivePort").write_text(
            f"{srv.port}\n", encoding="utf-8"
        )
        assert cdp.find_devtools_port(tmp_path, "Default") is None

        # With the browser WS path in line 2, discovery finds it cleanly.
        (tmp_path / "DevToolsActivePort").write_text(
            f"{srv.port}\n{srv.ws_path}\n", encoding="utf-8"
        )
        assert cdp.find_devtools_port(tmp_path, "Default") == srv.port
        assert cdp.devtools_port_is_ws_only(tmp_path, srv.port) is True

        state_path = profile_dir / "Preferences.dotbrave.settings.json"
        plan = Plan(
            namespace="settings",
            diff_lines=['  + "brave.tabs.vertical_tabs_enabled" = true'],
            apply_fn=lambda d: d["brave"]["tabs"].__setitem__(
                "vertical_tabs_enabled", True
            ),
            verify_fn=lambda _d: None,
            state_path=state_path,
            state_payload={
                "managed_keys": ["brave.tabs.vertical_tabs_enabled"],
                "prior_values": {},
            },
        )
        live.apply_live(
            srv.port, prefs_path, prefs, [plan], unattended=False, profile="Default"
        )

        methods = [c["method"] for c in srv.commands]
        assert "Target.createTarget" in methods
        create_cmd = next(
            c for c in srv.commands if c["method"] == "Target.createTarget"
        )
        assert create_cmd["params"].get("background") is True
        assert "Target.attachToTarget" in methods
        assert "Target.closeTarget" in methods
        eval_cmds = [c for c in srv.commands if c["method"] == "Runtime.evaluate"]
        assert all(c.get("sessionId") == "sess-1" for c in eval_cmds)
        assert state_path.exists()


def test_unattended_skips_ws_only_devtools_port(
    tmp_path: Path, monkeypatch
) -> None:
    import argparse
    from dotbrave._base import orchestrator

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text(json.dumps({"a": {"b": False}}))
    cfg = tmp_path / "brave.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    with _MockWsOnlyDevToolsServer(profile_dir) as srv:
        (tmp_path / "DevToolsActivePort").write_text(
            f"{srv.port}\n{srv.ws_path}\n", encoding="utf-8"
        )
        live_called = []
        orchestrator.cmd_apply(
            argparse.Namespace(
                profile_root=tmp_path,
                profile="Default",
                config=str(cfg),
                dry_run=False,
                unattended=True,
            ),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: None,
            restart_fn=lambda c: c,
            build_plans_fn=lambda p, prefs, doc, **kw: [
                orchestrator.Plan(
                    namespace="settings",
                    diff_lines=["  settings: change"],
                    apply_fn=lambda prefs: None,
                    verify_fn=lambda prefs: None,
                )
            ],
            live_apply_fn=lambda *a, **k: live_called.append(True),
            graceful_close_fn=lambda: None,
            launch_live_fn=lambda *a, **k: [],
        )
        assert live_called == []
        assert srv.commands == []


class _MockHttpAndWsDevToolsServer(_MockWsOnlyDevToolsServer):
    """HTTP + Browser WS server mimicking ``--remote-debugging-port``."""

    def _handle_conn(self, conn) -> None:
        import struct

        conn.settimeout(2.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        header_text = buf.decode("iso-8859-1", "replace")
        lines = header_text.split("\r\n")
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket":
            self.http_paths.append(path)
            if path == "/json/list":
                body = json.dumps([{"type": "page", "id": "t1"}]).encode("utf-8")
            elif path == "/json/version":
                body = json.dumps(
                    {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}{self.ws_path}"}
                ).encode("utf-8")
            else:
                conn.sendall(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            return
        # Delegate WebSocket handshake/frames by replaying against base logic:
        import base64
        import hashlib

        key = headers["sec-websocket-key"]
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

        def send_ws_json(obj: dict) -> None:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            hdr = bytearray([0x81])
            n = len(raw)
            if n < 126:
                hdr.append(n)
            elif n <= 0xFFFF:
                hdr.append(126)
                hdr.extend(struct.pack("!H", n))
            else:
                hdr.append(127)
                hdr.extend(struct.pack("!Q", n))
            conn.sendall(bytes(hdr) + raw)

        def read_exact(n: int) -> bytes | None:
            out = bytearray()
            while len(out) < n:
                ch = conn.recv(n - len(out))
                if not ch:
                    return None
                out.extend(ch)
            return bytes(out)

        while not self._stop.is_set():
            first = read_exact(2)
            if first is None or (first[0] & 0x0F) == 0x8:
                return
            length = first[1] & 0x7F
            masked = bool(first[1] & 0x80)
            if length == 126:
                ext = read_exact(2)
                if ext is None:
                    return
                length = struct.unpack("!H", ext)[0]
            mask = read_exact(4) if masked else b""
            if mask is None:
                return
            payload = read_exact(length)
            if payload is None:
                return
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            msg = json.loads(payload.decode("utf-8"))
            self.commands.append(msg)
            req_id = msg["id"]
            method = msg["method"]
            if method == "Target.createTarget":
                send_ws_json({"id": req_id, "result": {"targetId": "policy-tab"}})
            elif method == "Target.attachToTarget":
                send_ws_json({"id": req_id, "result": {"sessionId": "sess-pol"}})
            elif method == "Target.closeTarget":
                send_ws_json({"id": req_id, "result": {"success": True}})
            elif method == "Runtime.evaluate":
                send_ws_json(
                    {
                        "id": req_id,
                        "sessionId": msg.get("sessionId"),
                        "result": {"result": {"type": "boolean", "value": True}},
                    }
                )


def test_reload_policies_via_cdp_uses_background_tab_and_skips_ws_only(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()

    # 1) WS-only brave://inspect endpoint must NOT be opened for policy reload.
    with _MockWsOnlyDevToolsServer(profile_dir) as ws_srv:
        (tmp_path / "DevToolsActivePort").write_text(
            f"{ws_srv.port}\n{ws_srv.ws_path}\n", encoding="utf-8"
        )
        assert cdp.reload_policies_via_cdp(ws_srv.port, tmp_path) is False
        assert ws_srv.commands == []
    (tmp_path / "DevToolsActivePort").unlink()

    # 2) Fixed-port HTTP endpoint discovers /devtools/browser/<uuid> via
    # /json/version, opens chrome://policy in background, calls reloadPolicies,
    # and closes the tab cleanly.
    with _MockHttpAndWsDevToolsServer(profile_dir) as http_srv:
        assert cdp.reload_policies_via_cdp(http_srv.port, tmp_path) is True
        create_cmd = next(
            c for c in http_srv.commands if c["method"] == "Target.createTarget"
        )
        assert create_cmd["params"] == {"url": "chrome://policy", "background": True}
        assert any(
            c["method"] == "Runtime.evaluate"
            and "reloadPolicies" in c["params"]["expression"]
            for c in http_srv.commands
        )
        assert any(c["method"] == "Target.closeTarget" for c in http_srv.commands)
        assert not any(p.startswith("/json/new") for p in http_srv.http_paths)


def test_orchestrator_pwa_apply_uses_live_cdp_reload_on_windows(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import argparse
    from dotbrave._base import orchestrator as orch

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text("{}")
    cfg = tmp_path / "brave.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com"]\n')

    monkeypatch.setattr(orch.sys, "platform", "win32")
    monkeypatch.setattr(orch, "_already_privileged", lambda: True)

    with _MockHttpAndWsDevToolsServer(profile_dir) as srv:
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
            find_cmdline_fn=lambda: ["brave.exe", f"--remote-debugging-port={srv.port}"],
            restart_fn=lambda c: c,
            build_plans_fn=lambda p, prefs, doc, **kw: [
                orch.Plan(
                    namespace="pwa",
                    diff_lines=["  + https://example.com"],
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
        assert "picks it up within a few seconds" in out
        assert any(
            c["method"] == "Runtime.evaluate"
            and "reloadPolicies" in c["params"]["expression"]
            for c in srv.commands
        )


def test_create_page_prefers_background_browser_ws_over_http_json_new(
    tmp_path: Path,
) -> None:
    """Plain ``CdpClient(port).create_page(...)`` (with no ``ws_path`` passed
    upfront, as ``live.apply_live`` uses on a fixed ``--remote-debugging-port``)
    must discover ``/devtools/browser/<id>`` via ``/json/version`` and open the
    work tab with ``Target.createTarget(..., background=True)`` so it never
    steals focus via ``PUT /json/new``."""
    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()

    with _MockHttpAndWsDevToolsServer(profile_dir) as srv:
        client = cdp.CdpClient(srv.port)
        try:
            target = client.create_page("about:blank")
            assert target["_via_browser_ws"] is True
            create_cmd = next(
                c for c in srv.commands if c["method"] == "Target.createTarget"
            )
            assert create_cmd["params"] == {
                "url": "about:blank",
                "background": True,
            }
            assert not any(p.startswith("/json/new") for p in srv.http_paths)
            client.close_page(target)
        finally:
            client.close()


def test_ssh_and_non_tty_skip_ws_only_devtools_port(
    tmp_path: Path, monkeypatch
) -> None:
    """Attended runs over SSH (`ssh -tt`) or with non-TTY stdin must skip a
    WS-only `brave://inspect` DevToolsActivePort instead of hanging on an
    unanswerable GUI modal, while a local interactive TTY still uses it."""
    import argparse
    from dotbrave._base import orchestrator

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text(json.dumps({"a": {"b": False}}))
    cfg = tmp_path / "brave.toml"
    cfg.write_text('[settings]\n"a.b" = true\n')

    class FakeTtyStdin:
        def isatty(self) -> bool:
            return True

    class FakePipeStdin:
        def isatty(self) -> bool:
            return False

    with _MockWsOnlyDevToolsServer(profile_dir) as srv:
        (tmp_path / "DevToolsActivePort").write_text(
            f"{srv.port}\n{srv.ws_path}\n", encoding="utf-8"
        )

        # 1) SSH with PTY (`ssh -tt`): skipped -> falls through to relaunch.
        # 1) SSH with PTY (`ssh -tt`): skipped -> closes for offline apply +
        # restart (never GUI launch_live_fn, which user-enabled remote
        # debugging ignores in favor of the same WS-only server).
        monkeypatch.setattr(orchestrator.sys, "stdin", FakeTtyStdin())
        monkeypatch.setenv("SSH_CONNECTION", "100.64.0.1 55555 100.64.0.2 22")
        seen_ports: list[int] = []
        closed: list[bool] = []
        restarted: list[list[str]] = []
        live_launches: list[bool] = []
        orchestrator.cmd_apply(
            argparse.Namespace(
                profile_root=tmp_path,
                profile="Default",
                config=str(cfg),
                dry_run=False,
                unattended=False,
            ),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ["brave"],
            restart_fn=lambda c: (restarted.append(c), c)[1],
            build_plans_fn=lambda p, prefs, doc, **kw: [
                orchestrator.Plan(
                    namespace="settings",
                    diff_lines=["  settings: change"],
                    apply_fn=lambda prefs: prefs.__setitem__("a", {"b": True}),
                    verify_fn=lambda prefs: None,
                )
            ],
            live_apply_fn=lambda port, *a, **k: seen_ports.append(port),
            graceful_close_fn=lambda: closed.append(True),
            launch_live_fn=lambda *a, **k: (live_launches.append(True), ["brave"])[1],
        )
        assert closed == [True]
        assert seen_ports == []
        assert live_launches == []
        assert restarted == [["brave"]]
        assert srv.commands == []

        # 2) Non-TTY stdin without SSH: also skipped -> offline apply + restart.
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_TTY", raising=False)
        monkeypatch.setattr(orchestrator.sys, "stdin", FakePipeStdin())
        seen_ports.clear()
        closed.clear()
        restarted.clear()
        orchestrator.cmd_apply(
            argparse.Namespace(
                profile_root=tmp_path,
                profile="Default",
                config=str(cfg),
                dry_run=False,
                unattended=False,
            ),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ["brave"],
            restart_fn=lambda c: (restarted.append(c), c)[1],
            build_plans_fn=lambda p, prefs, doc, **kw: [
                orchestrator.Plan(
                    namespace="settings",
                    diff_lines=["  settings: change"],
                    apply_fn=lambda prefs: prefs.__setitem__("a", {"b": False}),
                    verify_fn=lambda prefs: None,
                )
            ],
            live_apply_fn=lambda port, *a, **k: seen_ports.append(port),
            graceful_close_fn=lambda: closed.append(True),
            launch_live_fn=lambda *a, **k: (live_launches.append(True), ["brave"])[1],
        )
        assert closed == [True]
        assert seen_ports == []
        assert live_launches == []
        assert restarted == [["brave"]]

        # 3) Local interactive TTY without SSH: uses WS-only port directly!
        monkeypatch.setattr(orchestrator.sys, "stdin", FakeTtyStdin())
        monkeypatch.setattr(
            orchestrator._process,
            "_windows_console_session_mismatch",
            lambda: False,
        )
        seen_ports.clear()
        closed.clear()
        orchestrator.cmd_apply(
            argparse.Namespace(
                profile_root=tmp_path,
                profile="Default",
                config=str(cfg),
                dry_run=False,
                unattended=False,
            ),
            display_name="Brave",
            running_fn=lambda: True,
            find_cmdline_fn=lambda: ["brave"],
            restart_fn=lambda c: c,
            build_plans_fn=lambda p, prefs, doc, **kw: [
                orchestrator.Plan(
                    namespace="settings",
                    diff_lines=["  settings: change"],
                    apply_fn=lambda prefs: None,
                    verify_fn=lambda prefs: None,
                )
            ],
            live_apply_fn=lambda port, *a, **k: seen_ports.append(port),
            graceful_close_fn=lambda: closed.append(True),
            launch_live_fn=lambda *a, **k: ["brave"],
        )
        assert closed == []
        assert seen_ports == [srv.port]


def test_ws_only_handshake_failure_fails_fast_without_double_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """When a WS-only `brave://inspect` handshake fails or is denied, live apply
    must raise `LiveApplyUnsupported` after a single WS attempt rather than
    swallowing `CdpError`, trying `/json/new`, and hanging a second time in
    `list_targets`."""
    import pytest
    from dotbrave import live
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan

    profile_dir = tmp_path / "Default"
    profile_dir.mkdir()
    prefs_path = profile_dir / "Preferences"
    prefs = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    ws_attempts: list[str] = []

    class RejectingWebSocket:
        def __init__(self, url: str, timeout: float = 5.0):
            ws_attempts.append(url)
            raise cdp.CdpError("DevTools websocket handshake failed (HTTP/1.1 403 Forbidden)")

    with _MockWsOnlyDevToolsServer(profile_dir) as srv:
        (tmp_path / "DevToolsActivePort").write_text(
            f"{srv.port}\n{srv.ws_path}\n", encoding="utf-8"
        )
        monkeypatch.setattr(cdp, "_WebSocket", RejectingWebSocket)
        plan = Plan(
            namespace="settings",
            diff_lines=['  + "brave.tabs.vertical_tabs_enabled" = true'],
            apply_fn=lambda d: None,
            verify_fn=lambda _d: None,
        )
        with pytest.raises(shared_live.LiveApplyUnsupported):
            live.apply_live(
                srv.port, prefs_path, prefs, [plan], unattended=False, profile="Default"
            )

        assert len(ws_attempts) == 1
        assert not any(p.startswith("/json/new") for p in srv.http_paths)
