"""Tiny Chrome DevTools Protocol client used by live apply.

This intentionally avoids a runtime dependency on websocket-client.  It
implements the small WebSocket subset needed for one request/response
CDP commands against a local browser endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

_LIVE_PORT_SIDECAR = ".dotbrave.live.json"


class CdpError(RuntimeError):
    """A DevTools request failed.

    Raised rather than exiting so a live adapter can fall back to an
    offline apply.  Every `sys.exit` this replaced aborted the run after
    a backup -- and possibly an external policy write -- had happened.
    """


class _WebSocket:
    def __init__(self, url: str, timeout: float = 5.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise CdpError(f"unsupported DevTools websocket URL: {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
            self._handshake()
        except (OSError, TimeoutError) as e:
            if hasattr(self, "sock"):
                self.close()
            raise CdpError(f"DevTools websocket connect failed: {e}") from e

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        header = response.decode("iso-8859-1", "replace")
        status_line = header.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise CdpError(f"DevTools websocket handshake failed ({status_line})")
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        headers = {}
        for line in header.split("\r\n")[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != expected:
            raise CdpError("DevTools websocket accept header mismatch")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(bytes(header) + masked)
        except (OSError, TimeoutError) as e:
            raise CdpError(f"DevTools websocket send failed: {e}") from e

    def recv_text(self) -> str:
        try:
            while True:
                first = self._read_exact(2)
                opcode = first[0] & 0x0F
                length = first[1] & 0x7F
                masked = bool(first[1] & 0x80)
                if length == 126:
                    length = struct.unpack("!H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact(8))[0]
                mask = self._read_exact(4) if masked else b""
                payload = self._read_exact(length)
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    raise CdpError("DevTools websocket closed")
                if opcode == 0x9:
                    self._send_pong(payload)
                    continue
                if opcode == 0x1:
                    return payload.decode("utf-8")
        except (OSError, TimeoutError) as e:
            raise CdpError(f"DevTools websocket recv failed: {e}") from e

    def _send_pong(self, payload: bytes) -> None:
        self.sock.sendall(bytes([0x8A, len(payload)]) + payload)

    def _read_exact(self, n: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < n:
            chunk = self.sock.recv(n - len(chunks))
            if not chunk:
                raise CdpError("DevTools websocket ended unexpectedly")
            chunks.extend(chunk)
        return bytes(chunks)


class CdpClient:
    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        ws_path: str | None = None,
    ):
        self.port = port
        self.host = host
        self._ws_path = ws_path
        self._browser_ws: _WebSocket | None = None
        self._next_id = 1

    def bind_profile_root(self, profile_root: Path) -> None:
        if self._ws_path is None:
            self._ws_path = read_devtools_ws_path(Path(profile_root), self.port)

    def close(self) -> None:
        if self._browser_ws is not None:
            self._browser_ws.close()
            self._browser_ws = None

    def _ensure_browser_ws(self) -> _WebSocket:
        if self._browser_ws is not None:
            return self._browser_ws
        if not self._ws_path:
            raise CdpError(
                f"no browser websocket path for {self.host}:{self.port}"
            )
        # 15s allows interactive approval on brave://inspect/#remote-debugging
        # endpoints when connecting for the first time.
        self._browser_ws = _WebSocket(
            f"ws://{self.host}:{self.port}{self._ws_path}", timeout=15.0
        )
        return self._browser_ws

    def _browser_command(
        self,
        method: str,
        params: dict | None = None,
        *,
        session_id: str | None = None,
    ) -> dict:
        ws = self._ensure_browser_ws()
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        try:
            ws.send_text(json.dumps(payload, separators=(",", ":")))
            while True:
                msg = json.loads(ws.recv_text())
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise CdpError(f"DevTools command failed: {msg['error']}")
                    return msg
        except CdpError:
            self.close()
            raise
        except json.JSONDecodeError as e:
            self.close()
            raise CdpError(f"DevTools websocket invalid JSON: {e}") from e

    def _json(self, path: str) -> Any:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise CdpError(
                f"could not reach DevTools endpoint at "
                f"{self.host}:{self.port}: {e}"
            )

    def list_targets(self) -> list[dict]:
        if self._browser_ws is None:
            try:
                targets = self._json("/json/list")
                return targets if isinstance(targets, list) else []
            except CdpError:
                if not self._ws_path:
                    raise
        res = self._browser_command("Target.getTargets").get("result", {})
        infos = res.get("targetInfos", [])
        out: list[dict] = []
        for info in infos if isinstance(infos, list) else []:
            if isinstance(info, dict) and isinstance(info.get("targetId"), str):
                tid = info["targetId"]
                out.append({**info, "id": tid, "_via_browser_ws": True})
        return out

    def create_page(self, url: str = "about:blank") -> dict:
        """Open a fresh page target (tab) and return its descriptor.

        Live apply drives privileged UI pages in a dedicated tab so no
        user tab gets navigated away; callers close it when done.
        """
        if self._browser_ws is None:
            path = f"/json/new?{urllib.parse.quote(url, safe=':/')}"
            req = urllib.request.Request(
                f"http://{self.host}:{self.port}{path}", method="PUT"
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    created = json.loads(resp.read().decode("utf-8"))
                if not isinstance(created, dict):
                    raise RuntimeError("unexpected /json/new response")
                return created
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError) as e:
                if not self._ws_path:
                    raise RuntimeError(f"could not open a DevTools work tab: {e}")

        # background=True keeps focus on the user's active window/tab.
        res = self._browser_command(
            "Target.createTarget", {"url": url, "background": True}
        ).get("result", {})
        tid = res.get("targetId")
        if not isinstance(tid, str) or not tid:
            raise RuntimeError("unexpected Target.createTarget response")
        sess = self._browser_command(
            "Target.attachToTarget", {"targetId": tid, "flatten": True}
        ).get("result", {})
        sid = sess.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise RuntimeError("Target.attachToTarget returned no sessionId")
        return {
            "id": tid,
            "targetId": tid,
            "type": "page",
            "url": url,
            "_session_id": sid,
            "_via_browser_ws": True,
        }

    def close_page(self, target: dict) -> None:
        """Best-effort close of a tab opened via ``create_page``."""
        target_id = target.get("id") or target.get("targetId")
        if not target_id:
            return
        if target.get("_via_browser_ws") or self._browser_ws is not None:
            try:
                self._browser_command(
                    "Target.closeTarget", {"targetId": target_id}
                )
            except CdpError:
                pass
            return
        try:
            urllib.request.urlopen(
                f"http://{self.host}:{self.port}/json/close/{target_id}",
                timeout=5,
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    def navigate(self, target: dict, url: str) -> None:
        self._command(target, "Page.navigate", {"url": url})

    def reload(self, target: dict) -> None:
        self._command(target, "Page.reload", {"ignoreCache": True})

    def evaluate(self, target: dict, expression: str) -> Any:
        msg = self._command(
            target,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        result = msg.get("result", {})
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            desc = detail.get("exception", {}).get("description") or detail.get("text")
            raise CdpError(f"DevTools evaluation failed: {desc}")
        value = result.get("result", {})
        return value.get("value")

    def _command(self, target: dict, method: str, params: dict | None = None) -> dict:
        if target.get("_via_browser_ws"):
            sid = target.get("_session_id")
            if not isinstance(sid, str) or not sid:
                tid = target.get("id") or target.get("targetId")
                if not isinstance(tid, str) or not tid:
                    raise CdpError("DevTools target has no targetId")
                sess = self._browser_command(
                    "Target.attachToTarget", {"targetId": tid, "flatten": True}
                ).get("result", {})
                sid = sess.get("sessionId")
                if not isinstance(sid, str) or not sid:
                    raise CdpError("DevTools target attach returned no sessionId")
                target["_session_id"] = sid
            return self._browser_command(method, params, session_id=sid)

        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url:
            raise CdpError("DevTools target has no websocket URL")
        ws = _WebSocket(ws_url)
        try:
            payload = {"id": 1, "method": method}
            if params is not None:
                payload["params"] = params
            ws.send_text(json.dumps(payload, separators=(",", ":")))
            while True:
                msg = json.loads(ws.recv_text())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise CdpError(f"DevTools command failed: {msg['error']}")
                    return msg
        finally:
            ws.close()


def devtools_endpoint_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=0.5
        ) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False
    return isinstance(targets, list)


#: Probes for a port the browser is recorded as serving, and the pause
#: between them.  Module-level so tests can drop the pause.
_RECORDED_PORT_PROBES = 4
_RECORDED_PORT_RETRY_S = 0.25


def _recorded_endpoint_alive(port: int) -> bool:
    """``devtools_endpoint_alive`` with retries, for a port the sidecar,
    ``DevToolsActivePort`` or the running command line says is served.

    This probe is all that stands between "use the endpoint" and "close
    and relaunch the browser", a restart that steals window focus, so one
    0.5s answer from a busy browser must not decide it.  A port nobody
    listens on refuses at once: a stale record costs under a second.
    """
    for attempt in range(_RECORDED_PORT_PROBES):
        if devtools_endpoint_alive(port):
            return True
        if attempt + 1 < _RECORDED_PORT_PROBES:
            time.sleep(_RECORDED_PORT_RETRY_S)
    return False


def devtools_ws_only_alive(port: int) -> bool:
    """True when ``127.0.0.1:port`` answers ``GET /json/list`` with HTTP 404.

    ``brave://inspect/#remote-debugging`` disables the ``/json/*`` HTTP
    routes while serving Browser-level CDP on the WebSocket path in line 2
    of ``DevToolsActivePort``.  Probing over HTTP avoids opening the
    WebSocket -- which prompts for interactive approval -- until needed.
    """
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=0.5
        ).close()
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        return code == 404
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return False


def remember_devtools_port(profile_root, profile: str, port: int) -> None:
    root = Path(profile_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / _LIVE_PORT_SIDECAR
    path.write_text(
        json.dumps({"profile": profile, "port": int(port)}, indent=2),
        encoding="utf-8",
    )


def _read_dotbrave_live_port(profile_root: Path, profile: str | None) -> int | None:
    path = profile_root / _LIVE_PORT_SIDECAR
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stored_profile = data.get("profile")
    if profile is not None and isinstance(stored_profile, str):
        if stored_profile != profile:
            return None
    try:
        port = int(data["port"])
    except (KeyError, TypeError, ValueError):
        return None
    return port if _recorded_endpoint_alive(port) else None


def _read_devtools_active_port_entry(
    profile_root: Path,
) -> tuple[int, str | None] | None:
    path = profile_root / "DevToolsActivePort"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    if port <= 0:
        return None
    ws_path = lines[1].strip() if len(lines) > 1 and lines[1].strip() else None
    return port, ws_path


def _read_devtools_active_port(profile_root: Path) -> int | None:
    entry = _read_devtools_active_port_entry(profile_root)
    return entry[0] if entry is not None else None


def read_devtools_ws_path(profile_root: Path, port: int) -> str | None:
    entry = _read_devtools_active_port_entry(Path(profile_root))
    if entry is None:
        return None
    active_port, ws_path = entry
    if (
        active_port != port
        or not ws_path
        or not ws_path.startswith("/devtools/browser/")
    ):
        return None
    return ws_path


def devtools_port_is_ws_only(profile_root, port: int) -> bool:
    if read_devtools_ws_path(Path(profile_root), port) is None:
        return False
    return not devtools_endpoint_alive(port) and devtools_ws_only_alive(port)


_PORT_FLAG = "--remote-debugging-port"


def _port_from_cmdline(args: list[str] | None) -> int | None:
    """The debugging port a running browser was actually started with.

    ``DevToolsActivePort`` is not a reliable source: the browser only
    writes that file when the port is dynamic (``--remote-debugging-port=0``).
    Started on a fixed port -- measured on both macOS and Linux -- the file
    never appears, so a browser that *is* serving an endpoint reads as
    having none, and gets closed for no reason. Its command line still
    carries the number.
    """
    if not args:
        return None
    for i, arg in enumerate(args):
        raw: str | None = None
        if arg.startswith(_PORT_FLAG + "="):
            raw = arg.split("=", 1)[1]
        elif arg == _PORT_FLAG and i + 1 < len(args):
            raw = args[i + 1]
        if raw is None:
            continue
        try:
            port = int(raw)
        except ValueError:
            return None
        # 0 means "pick one for me": the real port lives in
        # DevToolsActivePort, which the caller already tried.
        return port or None
    return None


def find_devtools_port(profile_root, profile: str | None = None) -> int | None:
    """Endpoint recorded on disk: dotbrave's own sidecar, then the
    browser's ``DevToolsActivePort``.  See ``live_port_from_cmdline`` for
    the case neither file covers."""
    root = Path(profile_root)
    sidecar_port = _read_dotbrave_live_port(root, profile)
    if sidecar_port is not None:
        return sidecar_port
    entry = _read_devtools_active_port_entry(root)
    if entry is None:
        return None
    active_port, ws_path = entry
    if _recorded_endpoint_alive(active_port):
        return active_port
    if (
        ws_path
        and ws_path.startswith("/devtools/browser/")
        and devtools_ws_only_alive(active_port)
    ):
        return active_port
    return None


def live_port_from_cmdline(
    cmdline_fn: Callable[[], list[str] | None] | None,
) -> int | None:
    """Live endpoint of a browser started on a fixed debugging port.

    Neither file source sees that case -- the browser writes
    ``DevToolsActivePort`` only for a dynamic port, and dotbrave's
    sidecar only exists for endpoints dotbrave itself started.  The
    running process still knows, so ask it.  A port that does not answer
    is discarded, and any failure to read the command line is treated as
    "no endpoint": discovery must never be the thing that breaks apply.
    """
    if cmdline_fn is None:
        return None
    try:
        port = _port_from_cmdline(cmdline_fn())
    except Exception:  # noqa: BLE001 - discovery must never break apply
        return None
    if port is None:
        return None
    return port if _recorded_endpoint_alive(port) else None


def pick_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_devtools_endpoint(
    port: int,
    display_name: str,
    *,
    timeout: float = 15.0,
) -> None:
    import time

    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            targets = CdpClient(port).list_targets()
            if targets:
                return
        except CdpError as e:
            last_error = str(e)
        time.sleep(0.25)
    sys.exit(
        f"error: {display_name} did not expose a DevTools endpoint on "
        f"127.0.0.1:{port} within {timeout:.0f}s"
        + (f"\nlast error: {last_error}" if last_error else "")
    )
