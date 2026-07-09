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
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_LIVE_PORT_SIDECAR = ".dotbrave.live.json"


class _WebSocket:
    def __init__(self, url: str, timeout: float = 5.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            sys.exit(f"error: unsupported DevTools websocket URL: {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._handshake()

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
        if " 101 " not in header.split("\r\n", 1)[0]:
            sys.exit("error: DevTools websocket handshake failed")
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
            sys.exit("error: DevTools websocket accept header mismatch")

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
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
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
                sys.exit("error: DevTools websocket closed")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0x1:
                return payload.decode("utf-8")

    def _send_pong(self, payload: bytes) -> None:
        self.sock.sendall(bytes([0x8A, len(payload)]) + payload)

    def _read_exact(self, n: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < n:
            chunk = self.sock.recv(n - len(chunks))
            if not chunk:
                sys.exit("error: DevTools websocket ended unexpectedly")
            chunks.extend(chunk)
        return bytes(chunks)


class CdpClient:
    def __init__(self, port: int, *, host: str = "127.0.0.1"):
        self.port = port
        self.host = host

    def _json(self, path: str) -> Any:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            sys.exit(
                f"error: could not reach DevTools endpoint at "
                f"{self.host}:{self.port}: {e}"
            )

    def list_targets(self) -> list[dict]:
        targets = self._json("/json/list")
        return targets if isinstance(targets, list) else []

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
            sys.exit(f"error: DevTools evaluation failed: {desc}")
        value = result.get("result", {})
        return value.get("value")

    def _command(self, target: dict, method: str, params: dict | None = None) -> dict:
        ws_url = target.get("webSocketDebuggerUrl")
        if not isinstance(ws_url, str) or not ws_url:
            sys.exit("error: DevTools target has no websocket URL")
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
                        sys.exit(f"error: DevTools command failed: {msg['error']}")
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
    return port if devtools_endpoint_alive(port) else None


def _read_devtools_active_port(profile_root: Path) -> int | None:
    path = profile_root / "DevToolsActivePort"
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    try:
        return int(first)
    except ValueError:
        return None


def find_devtools_port(profile_root, profile: str | None = None) -> int | None:
    root = Path(profile_root)
    sidecar_port = _read_dotbrave_live_port(root, profile)
    if sidecar_port is not None:
        return sidecar_port
    active_port = _read_devtools_active_port(root)
    if active_port is None:
        return None
    return active_port if devtools_endpoint_alive(active_port) else None


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
        except SystemExit as e:
            last_error = str(e)
        time.sleep(0.25)
    sys.exit(
        f"error: {display_name} did not expose a DevTools endpoint on "
        f"127.0.0.1:{port} within {timeout:.0f}s"
        + (f"\nlast error: {last_error}" if last_error else "")
    )
