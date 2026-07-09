"""Generic browser-process management, parameterized by BrowserProcess config."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _read_cmdline(pid: str) -> list[str] | None:
    """Recover the command-line argv for a running process.

    Platform-specific but NOT browser-specific.
    """
    if _is_windows():
        try:
            out = subprocess.check_output(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
                ],
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        line = out.decode("utf-8", "replace").strip()
        return [line] if line else None
    if _is_macos():
        try:
            out = subprocess.check_output(
                ["ps", "-o", "command=", "-p", pid],
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        line = out.decode("utf-8", "replace").strip()
        return [line] if line else None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError):
        return None
    parts = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")]
    if len(parts) == 1 and " " in parts[0]:
        return shlex.split(parts[0])
    return parts


class BrowserProcess:
    """Browser process management configured for a specific browser.

    All platform-specific process detection, kill, and restart logic
    lives here.  Each browser module creates one instance with its
    specific names and paths.
    """

    def __init__(
        self,
        *,
        display_name: str,
        proc_name_linux: str,
        proc_name_macos: str,
        proc_name_windows: str,
        macos_app_name: str,
        linux_wrappers: list[str],
        windows_exe_relpath: tuple[str, ...],
        flatpak_prefix: str | None = None,
        flatpak_app_id: str | None = None,
        linux_pid_filter: str | None = None,
    ):
        self.display_name = display_name
        self.proc_name_linux = proc_name_linux
        self.proc_name_macos = proc_name_macos
        self.proc_name_windows = proc_name_windows
        self.macos_app_name = macos_app_name
        self.linux_wrappers = linux_wrappers
        self.windows_exe_relpath = windows_exe_relpath
        self.flatpak_prefix = flatpak_prefix
        self.flatpak_app_id = flatpak_app_id
        # Linux-only argv[0] discriminator for browsers whose channels
        # share a basename (e.g. all Brave channels install with the
        # binary named "brave").  Pids whose argv[0] does not contain
        # this substring are dropped from `pids()` and `running()` so
        # `pkill -KILL -x` only fires on processes the user actually
        # asked about.  None disables the filter (the default; keeps
        # behavior unchanged for browsers without per-channel paths).
        self.linux_pid_filter = linux_pid_filter

    def proc_name(self) -> str:
        if _is_macos():
            return self.proc_name_macos
        if _is_windows():
            return self.proc_name_windows
        return self.proc_name_linux

    def _pids_windows(self) -> list[str]:
        name = self.proc_name()
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}",
                 "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        pids: list[str] = []
        for line in out.decode("utf-8", "replace").strip().splitlines():
            if line.startswith(f'"{name}"'):
                parts = line.split(",")
                if len(parts) >= 2:
                    pids.append(parts[1].strip('"'))
        return pids

    def running(self) -> bool:
        if _is_windows():
            return bool(self._pids_windows())
        return bool(self.pids())

    def _apply_linux_filter(self, pids: list[str]) -> list[str]:
        """Drop pids whose argv[0] does not contain ``linux_pid_filter``.

        Only invoked on Linux when the filter is set -- macOS uses
        channel-distinct proc names already, and Windows uses
        channel-distinct exe paths.  Pids whose cmdline can't be read
        (raced exit, EPERM) are dropped; conservative (don't kill
        what we can't identify).
        """
        if self.linux_pid_filter is None:
            return pids
        kept: list[str] = []
        for pid in pids:
            args = _read_cmdline(pid)
            if not args:
                continue
            if self.linux_pid_filter in args[0]:
                kept.append(pid)
        return kept

    def pids(self) -> list[str]:
        if _is_windows():
            return self._pids_windows()
        try:
            out = subprocess.check_output(
                ["pgrep", "-x", self.proc_name()], stderr=subprocess.DEVNULL
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        raw = out.decode().split()
        if not _is_macos() and self.linux_pid_filter is not None:
            return self._apply_linux_filter(raw)
        return raw

    def find_main_cmdline(self) -> list[str] | None:
        """The main browser process is the one without ``--type=...``."""
        for pid in self.pids():
            args = _read_cmdline(pid)
            if not args:
                continue
            if any("--type=" in a for a in args):
                continue
            return args
        return None

    def kill_and_wait(self, timeout: float = 5.0) -> None:
        if _is_windows():
            subprocess.run(
                ["taskkill", "/F", "/IM", self.proc_name()],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif not _is_macos() and self.linux_pid_filter is not None:
            # Channel-scoped kill: don't `pkill -x brave` (matches every
            # channel) -- send SIGKILL only to the pids we already
            # filtered to this channel.
            scoped = self.pids()
            if scoped:
                subprocess.run(
                    ["kill", "-KILL", *scoped],
                    stderr=subprocess.DEVNULL,
                )
        else:
            subprocess.run(
                ["pkill", "-KILL", "-x", self.proc_name()],
                stderr=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return
            time.sleep(0.1)
        sys.exit(
            f"error: {self.display_name} still running after "
            f"force-kill + {timeout}s wait"
        )

    def close_and_wait(self, timeout: float = 15.0) -> None:
        if _is_windows():
            subprocess.run(
                ["taskkill", "/IM", self.proc_name()],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif _is_macos():
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "{self.macos_app_name}" to quit',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif self.linux_pid_filter is not None:
            scoped = self.pids()
            if scoped:
                subprocess.run(
                    ["kill", "-TERM", *scoped],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            subprocess.run(
                ["pkill", "-TERM", "-x", self.proc_name()],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return
            time.sleep(0.1)
        sys.exit(
            f"error: {self.display_name} is still running after a normal "
            "close request. Close it manually and retry."
        )

    def _is_flatpak_cmdline(self, captured_cmdline: list[str]) -> bool:
        if self.flatpak_prefix is None:
            return False
        return bool(captured_cmdline) and captured_cmdline[0].startswith(
            self.flatpak_prefix
        )

    def restart(self, captured_cmdline: list[str]) -> list[str]:
        if _is_windows():
            local = os.environ.get("LOCALAPPDATA", "")
            known_exe = Path(local).joinpath(*self.windows_exe_relpath)
            if local and known_exe.exists():
                cmdline = [str(known_exe)]
            else:
                cmdline = list(captured_cmdline)
        elif _is_macos():
            cmdline = ["open", "-a", self.macos_app_name]
            forwarded = captured_cmdline[1:]
            if forwarded:
                cmdline += ["--args", *forwarded]
        elif self._is_flatpak_cmdline(captured_cmdline) and self.flatpak_app_id:
            cmdline = [
                "flatpak", "run", self.flatpak_app_id,
                *captured_cmdline[1:],
            ]
        else:
            wrapper = None
            for w in self.linux_wrappers:
                wrapper = shutil.which(w)
                if wrapper:
                    break
            if wrapper:
                cmdline = [wrapper, *captured_cmdline[1:]]
            else:
                cmdline = list(captured_cmdline)
        subprocess.Popen(
            cmdline,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return cmdline

    def live_launch_cmdline(
        self,
        profile_root: Path,
        profile: str,
        port: int,
        url: str | None = None,
    ) -> list[str]:
        flags = [
            f"--user-data-dir={profile_root}",
            f"--profile-directory={profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ]
        if _is_windows():
            local = os.environ.get("LOCALAPPDATA", "")
            known_exe = Path(local).joinpath(*self.windows_exe_relpath)
            if local and known_exe.exists():
                cmdline = [str(known_exe), *flags]
            else:
                found = shutil.which(self.proc_name())
                if found is None:
                    raise FileNotFoundError(self.proc_name())
                cmdline = [found, *flags]
        elif _is_macos():
            cmdline = ["open", "-a", self.macos_app_name, "--args", *flags]
        else:
            wrapper = None
            for w in self.linux_wrappers:
                wrapper = shutil.which(w)
                if wrapper:
                    break
            if wrapper is None:
                raise FileNotFoundError(self.linux_wrappers[0])
            cmdline = [wrapper, *flags]
        if url:
            cmdline.append(url)
        return cmdline

    def launch_live(
        self,
        profile_root: Path,
        profile: str,
        port: int,
        url: str | None = None,
    ) -> list[str]:
        cmdline = self.live_launch_cmdline(profile_root, profile, port, url)
        subprocess.Popen(
            cmdline,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return cmdline
