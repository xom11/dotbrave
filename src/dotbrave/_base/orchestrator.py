"""Shared orchestration logic for the dotbrave CLI.

Handles TOML loading (file + URL), the unified ``apply`` cycle
(preflight -> kill -> backup -> write -> verify -> restart), the
``init`` command, and the ``register_actions()`` argparse setup.

Process-interaction callbacks (running, kill, restart) are passed in
from each browser module so that tests can monkeypatch them at the
browser-module level.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotbrave._base.cdp import (
    find_devtools_port,
    pick_unused_port,
    remember_devtools_port,
    wait_for_devtools_endpoint,
)
from dotbrave._base.live_apply import (
    LiveApplyUnsupported,
    apply_external_plans,
    write_state_files,
)
from dotbrave._base import process as _process
from dotbrave._base.utils import (
    Plan,
    backup_prefs,
    find_preferences,
    load_prefs,
    restore_prefs,
    write_atomic,
)


_MAX_URL_CONFIG_BYTES = 256 * 1024
_HELP_FORMATTER = argparse.RawDescriptionHelpFormatter


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        sys.exit(f"error: config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"error: invalid TOML at {path}: {e}")


def _looks_like_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _load_toml_from_url(
    url: str,
    *,
    allow_http: bool = False,
    expect_sha256: str | None = None,
) -> dict:
    if url.startswith("http://") and not allow_http:
        sys.exit(
            f"error: refusing to fetch config over plain http: {url}\n"
            "  HTTP responses can be modified by anyone on the network and "
            "could inject\n"
            "  a malicious [pwa] table that runs through sudo. Use https:// "
            "or pass\n"
            "  --allow-http to opt in (e.g. for a trusted intranet host)."
        )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read(_MAX_URL_CONFIG_BYTES + 1)
    except urllib.error.URLError as e:
        sys.exit(f"error: failed to fetch {url}: {e.reason}")
    if len(data) > _MAX_URL_CONFIG_BYTES:
        sys.exit(
            f"error: config from {url} exceeds the {_MAX_URL_CONFIG_BYTES}-byte "
            f"limit. If this is intentional, fetch the file locally and pass "
            f"the path instead."
        )
    digest = hashlib.sha256(data).hexdigest()
    print(f"source: {url}")
    print(f"  size:   {len(data)} bytes")
    print(f"  sha256: {digest}")
    if expect_sha256 is not None:
        want = expect_sha256.strip().lower()
        if digest != want:
            sys.exit(
                f"error: sha256 mismatch for {url}\n"
                f"  expected: {want}\n"
                f"  got:      {digest}\n"
                "  refusing to apply -- the file may have changed or been "
                "tampered with."
            )
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        sys.exit(f"error: failed to parse TOML from {url}: {e}")


def load_toml_source(
    src: str,
    *,
    allow_http: bool = False,
    expect_sha256: str | None = None,
) -> dict:
    """Load a TOML config from a file path or URL."""
    if _looks_like_url(src):
        return _load_toml_from_url(
            src, allow_http=allow_http, expect_sha256=expect_sha256
        )
    return _load_toml(Path(src))


def _running_state(
    running_fn: Callable[[], bool],
    *,
    display_name: str,
    unattended: bool,
) -> bool | None:
    """``running_fn()``, with a third answer: None means "don't proceed".

    ``running_fn`` raises :class:`ProcessStateUnknown` when the OS
    process-listing tool is missing and the browser's state therefore
    cannot be read.  Both callers commit a Preferences write shortly
    after this point, and a write made underneath a live browser is
    silently undone -- the browser flushes its own in-memory copy back
    over the file.  So neither caller may guess:

    - ``--unattended`` reports on stderr and returns None, which the
      caller turns into an exit-0 skip.  That is the flag's standing
      contract: report and skip anything it cannot do safely.
    - otherwise this exits, naming the tool that is missing.

    Both paths return *before* any backup is taken.  A backup for a
    write that never happens is pure litter -- the observed failure left
    16 of them, ~200 KB each, on one machine.
    """
    try:
        return running_fn()
    # Looked up through the module, not imported by name: several tests
    # `importlib.reload` _base.process, which rebinds the class to a new
    # object.  A name imported here would then be the pre-reload class and
    # would stop matching what a reloaded BrowserProcess raises.
    except _process.ProcessStateUnknown as e:
        if unattended:
            print(
                f"unattended: cannot tell whether {display_name} is running "
                f"({e.tool} was not found on PATH); skipping. Writing "
                f"Preferences now could be silently undone by a running "
                f"{display_name}.",
                file=sys.stderr,
            )
            return None
        sys.exit(
            f"error: cannot determine whether {display_name} is running -- "
            f"{e.tool} was not found on PATH.\n"
            f"  Refusing to guess. Applying offline while {display_name} "
            f"might be running is silently\n"
            f"  undone: {display_name} holds Preferences in memory and "
            f"flushes its own copy back over\n"
            f"  the file, so the change disappears while the command reports "
            f"success.\n"
            f"  Put {e.tool} on PATH (macOS: /usr/bin; Linux: the procps "
            f"package) and retry."
        )


def cmd_apply(
    args: argparse.Namespace,
    *,
    display_name: str,
    running_fn: Callable[[], bool],
    find_cmdline_fn: Callable[[], list[str] | None],
    restart_fn: Callable[[list[str]], list[str]],
    build_plans_fn: Callable,
    live_apply_fn: Callable[[int, Path, dict, list], None] | None = None,
    graceful_close_fn: Callable[[], None] | None = None,
    launch_live_fn: Callable[[Path, str, int, str | None], list[str]] | None = None,
) -> None:
    """Unified apply orchestrator.

    Process callbacks are resolved at call time in each browser's
    ``cmd_apply`` wrapper, so test monkeypatching of the browser
    module's function names takes effect.
    """
    prefs_path = find_preferences(args.profile_root, args.profile)
    doc = load_toml_source(
        args.config,
        allow_http=getattr(args, "allow_http", False),
        expect_sha256=getattr(args, "expect_sha256", None),
    )
    if not isinstance(doc, dict):
        sys.exit("error: TOML root must be a table")

    prefs = load_prefs(prefs_path)
    plans = build_plans_fn(
        prefs_path, prefs, doc, skip=tuple(getattr(args, "skip", ()) or ())
    )

    if not plans:
        sys.exit(
            "error: config has no [shortcuts], [settings] or [pwa] table "
            "-- nothing to apply"
        )

    non_empty = [p for p in plans if not p.empty]
    if not non_empty:
        print("no changes -- Preferences already match config")
        return

    print(f"target: {prefs_path}")
    for plan in plans:
        for warning in plan.warnings:
            print(warning)
    for plan in non_empty:
        print(f"{plan.namespace}:")
        print("\n".join(plan.diff_lines))

    if args.dry_run:
        print("\n(dry-run, nothing written)")
        return

    unattended = getattr(args, "unattended", False)

    def _needs_root(p) -> bool:
        return p.external_apply_fn is not None and not p.empty

    if unattended and any(_needs_root(p) for p in plans):
        names = ", ".join(f"[{p.namespace}]" for p in plans if _needs_root(p))
        print(
            f"unattended: skipping {names} -- it needs elevated privileges. "
            f"Run `dotbrave apply` from a terminal to apply it.",
            file=sys.stderr,
        )
        plans = [p for p in plans if not _needs_root(p)]
        non_empty = [p for p in plans if not p.empty]
        if not non_empty:
            return

    needs_escalation = any(
        p.external_apply_fn is not None and not p.empty for p in plans
    )
    if needs_escalation:
        if sys.platform == "win32":
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                sys.exit(
                    "error: [pwa] requires administrator privileges to write "
                    "to the Windows Registry.\n"
                    "Re-run this command from an elevated (Administrator) "
                    "command prompt or PowerShell."
                )
        else:
            try:
                cached = subprocess.run(
                    ["sudo", "-n", "true"], stderr=subprocess.DEVNULL
                ).returncode == 0
                if not cached:
                    subprocess.run(["sudo", "-v"], check=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                sys.exit(
                    "error: [pwa] requires sudo to write the managed-policy "
                    f"file but auth failed: {e}\n"
                    "(if sudo isn't installed, [pwa] isn't supported on this "
                    "platform; if running non-interactively, run `sudo -v` "
                    "from a terminal first to cache credentials)"
                )

    saved_cmdline: list[str] | None = None
    was_closed = False
    relaunch_live_port: int | None = None
    is_running = _running_state(
        running_fn, display_name=display_name, unattended=unattended
    )
    if is_running is None:
        return
    if is_running:
        if all(p.external_apply_fn is not None for p in non_empty):
            # Every requested change lives in external managed policy
            # ([pwa]): nothing touches Preferences and nothing needs the
            # DevTools endpoint, so leave the running browser alone. The
            # browser reads the policy on its next launch.
            apply_external_plans(plans)
            write_state_files(plans)
            names = ", ".join(f"[{p.namespace}]" for p in non_empty)
            print(
                f"ok -- {names} policy written without touching the "
                f"running {display_name} (loaded at its next launch)"
            )
            return
        if live_apply_fn is not None:
            live_port = find_devtools_port(args.profile_root, args.profile)
            if live_port is None:
                if unattended:
                    print(
                        "unattended: Brave is running without a live "
                        "endpoint; skipping rather than closing it. Run "
                        "`dotbrave apply` from a terminal, or apply while "
                        "Brave is closed.",
                        file=sys.stderr,
                    )
                    return
                if graceful_close_fn is None or launch_live_fn is None:
                    sys.exit(
                        f"error: {display_name} is running but cannot be "
                        f"re-launched for live apply"
                    )
                live_port = pick_unused_port()
                print(
                    f"{display_name} is running without a live endpoint; "
                    f"closing it normally and relaunching once for live apply "
                    f"(no force-kill)."
                )
                graceful_close_fn()
                used = launch_live_fn(args.profile_root, args.profile, live_port, None)
                print(
                    f"relaunching {display_name} with live endpoint: "
                    f"{' '.join(map(str, used))}"
                )
                wait_for_devtools_endpoint(live_port, display_name)
            live_port = int(live_port)
            try:
                live_apply_fn(live_port, prefs_path, prefs, plans)
            except LiveApplyUnsupported as e:
                settings = "\n".join(f"  {key}" for key in e.keys)
                if unattended:
                    print(
                        "unattended: Brave cannot apply these settings live "
                        "and closing it is not allowed:\n" + settings,
                        file=sys.stderr,
                    )
                    return
                if graceful_close_fn is None or launch_live_fn is None:
                    sys.exit(
                        f"error: {e.browser_name} cannot apply these settings "
                        f"live and cannot be re-launched for offline apply:\n"
                        f"{settings}"
                    )
                print(
                    f"{display_name} cannot apply every requested setting "
                    "live; closing it normally for offline apply "
                    "(no force-kill)."
                )
                print(settings)
                graceful_close_fn()
                relaunch_live_port = live_port
                was_closed = True
            else:
                remember_devtools_port(args.profile_root, args.profile, live_port)
                return
        elif graceful_close_fn is None:
            sys.exit(
                f"error: {display_name} is running but dotbrave cannot "
                "request a normal close for offline apply."
            )
        elif relaunch_live_port is None:
            if unattended:
                print(
                    "unattended: Brave is running and offline apply would "
                    "close it; skipping.",
                    file=sys.stderr,
                )
                return
            saved_cmdline = find_cmdline_fn()
            print(f"closing {display_name} normally for offline apply")
            graceful_close_fn()
            was_closed = True

    backup = prefs_path.with_suffix(
        prefs_path.suffix + f".bak.{datetime.now():%Y%m%d-%H%M%S}"
    )
    backup_prefs(prefs_path, backup)
    print(f"backup: {backup}")

    # In-memory mutate first; nothing is on disk yet.
    for plan in plans:
        plan.apply_fn(prefs)

    # External (privileged) writes go BEFORE write_atomic so a sudo / I/O
    # failure here leaves Preferences unchanged.  The previous ordering
    # left prefs committed but the policy file un-applied if sudo
    # flaked, breaking the "single cycle" promise.
    #
    # Skip empty plans: needs_escalation above only checks non-empty plans
    # for sudo, so calling external_apply_fn on an empty plan here would
    # invoke sudo without the cache check -- and crash in non-interactive
    # contexts (e.g. home-manager activation) where sudo isn't on PATH.
    for plan in plans:
        if plan.external_apply_fn is not None and not plan.empty:
            plan.external_apply_fn()

    write_atomic(prefs_path, prefs)

    for plan in plans:
        if plan.state_path is not None:
            plan.state_path.write_text(
                json.dumps(plan.state_payload, indent=2), encoding="utf-8",
            )

    reloaded = load_prefs(prefs_path)
    for plan in plans:
        plan.verify_fn(reloaded)
    print("ok -- applied and verified")

    if relaunch_live_port is not None and launch_live_fn is not None:
        used = launch_live_fn(
            args.profile_root, args.profile, relaunch_live_port, None
        )
        print(
            f"relaunching {display_name} with live endpoint: "
            f"{' '.join(map(str, used))}"
        )
        wait_for_devtools_endpoint(relaunch_live_port, display_name)
        remember_devtools_port(
            args.profile_root, args.profile, relaunch_live_port
        )
    elif saved_cmdline:
        used = restart_fn(saved_cmdline)
        print(f"restarting {display_name}: {' '.join(used)}")
    elif was_closed:
        print(
            f"{display_name} closed; could not capture original "
            f"command line -- restart manually."
        )


_RESTORE_SIDECAR_NAMES = (
    "Preferences.dotbrave.shortcuts.json",
    "Preferences.dotbrave.settings.json",
)


def cmd_restore(
    args: argparse.Namespace,
    *,
    display_name: str,
    running_fn: Callable[[], bool],
    find_cmdline_fn: Callable[[], list[str] | None],
    restart_fn: Callable[[list[str]], list[str]],
    graceful_close_fn: Callable[[], None],
) -> None:
    """Restore Preferences from a backup created by a prior ``apply``.

    Resolves a backup file (most recent by mtime, or the one passed via
    ``--from``), copies it back over Preferences, and clears the
    shortcuts/settings sidecars so the next apply starts from a clean
    "managed by dotbrave" set.

    [pwa] is intentionally out of scope -- the policy file lives outside
    the profile and isn't part of the per-apply backup.  The user is
    told to edit the managed-policy file manually if they regret a pwa
    write.
    """
    prefs_path = find_preferences(args.profile_root, args.profile)
    profile_dir = prefs_path.parent
    backups = sorted(
        profile_dir.glob(f"{prefs_path.name}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if args.list:
        if not backups:
            print(f"no backups found next to {prefs_path}")
            return
        print(f"backups for {prefs_path}:")
        for bk in backups:
            ts = datetime.fromtimestamp(bk.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(f"  {bk.name}  {ts}  ({bk.stat().st_size} bytes)")
        return

    if args.from_path:
        backup = Path(args.from_path)
        if not backup.exists():
            sys.exit(f"error: backup not found: {backup}")
    else:
        if not backups:
            sys.exit(
                f"error: no backups found next to {prefs_path}.\n"
                "(`apply` writes a timestamped backup on every run; "
                "if you've never applied, there's nothing to restore.)"
            )
        backup = backups[0]

    print(f"target:  {prefs_path}")
    print(f"restore: {backup}")

    if args.dry_run:
        print("\n(dry-run, nothing written)")
        return

    saved_cmdline: list[str] | None = None
    was_closed = False
    # `restore` copies a backup over Preferences, so it loses the same
    # race `apply` does when the browser turns out to be running.  Same
    # refusal, same wording; `--unattended` reaches here through
    # `apply --undo`.
    is_running = _running_state(
        running_fn,
        display_name=display_name,
        unattended=getattr(args, "unattended", False),
    )
    if is_running is None:
        return
    if is_running:
        saved_cmdline = find_cmdline_fn()
        print(f"closing {display_name} normally for restore")
        graceful_close_fn()
        was_closed = True

    restore_prefs(backup, prefs_path)
    print(f"restored Preferences from {backup.name}")

    # Clear sidecars so the next `apply` doesn't think the restored
    # values are still under dotbrave management -- they were the
    # PRE-managed state when the backup was taken.
    for sidecar in _RESTORE_SIDECAR_NAMES:
        sp = profile_dir / sidecar
        if sp.exists():
            sp.unlink()
            print(f"cleared {sidecar}")

    print(
        "note: [pwa] policy file is NOT affected by restore. If you "
        "applied PWAs you no longer want, edit the managed-policy file "
        "(see `dotbrave pwa dump` for its location) manually."
    )

    if saved_cmdline:
        used = restart_fn(saved_cmdline)
        print(f"restarting {display_name}: {' '.join(used)}")
    elif was_closed:
        print(
            f"{display_name} closed; could not capture original "
            f"command line -- restart manually."
        )


_EXPORT_HEADER_NOTES = (
    "# This file captures user-visible customizations from your current",
    "# profile + managed-policy file:",
    "#",
    "#   [shortcuts] -- bindings that differ from Brave's defaults.",
    "#   [settings]  -- well-known user-facing keys, keys dotbrave manages,",
    "#                  and keys changed since the last `dotbrave export",
    "#                  --snapshot`.",
    "#   [pwa]       -- URLs currently force-installed via the managed-policy",
    "#                  file / Windows registry.",
    "#",
    "# To capture ANY setting you change in the browser UI: run `dotbrave",
    "# export --snapshot`, change settings, then re-run `dotbrave export`.",
    "#",
    "# Apply this file with: `dotbrave apply <this file>`",
)


def cmd_export(
    args: argparse.Namespace,
    *,
    browser_name: str,
    builders,
) -> None:
    """Emit a single TOML file capturing user-customized state.

    ``builders`` is a list of callables, each ``(args, prefs_path, prefs)
    -> list[str] | None``.  Each builder returns the lines for one
    namespace block (e.g. ``[shortcuts]`` or ``[pwa]``) or ``None`` to
    skip.  The orchestrator joins them with blank-line separators and
    writes either to stdout or to ``args.output``.
    """
    prefs_path = find_preferences(args.profile_root, args.profile)
    prefs = load_prefs(prefs_path)

    head: list[str] = ["# Generated by `dotbrave export`"]
    head.extend(_EXPORT_HEADER_NOTES)
    head.append(f"# Source profile: {prefs_path}")

    blocks: list[str] = []
    for builder in builders:
        block = builder(args, prefs_path, prefs)
        if block:
            blocks.append("\n".join(block))

    out = "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(out)


def cmd_init(args: argparse.Namespace, browser_name: str, template: str) -> None:
    filename = args.output or f"{browser_name}.toml"
    text = template.replace("{filename}", filename)
    if args.output:
        dest = Path(args.output)
        if dest.exists():
            sys.exit(f"error: {dest} already exists -- refusing to overwrite")
        dest.write_text(text, encoding="utf-8")
        print(f"wrote {dest}")
    else:
        sys.stdout.write(text)


def register_actions(
    parser: argparse.ArgumentParser,
    *,
    display_name: str,
    namespaces: tuple[str, ...],
    cmd_apply_fn,
    cmd_init_fn=None,
    cmd_restore_fn=None,
    cmd_export_fn=None,
    export_has_shortcuts: bool = False,
    module_registers: list,
    setup_profile_args: Callable[..., None],
    normalize_args: Callable[[argparse.Namespace], None] | None = None,
) -> None:
    """Mount the full action tree directly on the root parser.

    ``setup_profile_args(parser, leaf=...)`` attaches the profile flags
    (``--channel`` / ``--profile-root`` / ``--profile``) twice: on the
    root parser with real defaults (flags before the action), and on
    each profile-reading action parser with ``argparse.SUPPRESS``
    defaults so a flag given after the action overrides the root value
    instead of resetting it. Leaf parsers also mark their namespace as
    profile-dependent so ``normalize_args`` can skip profile-root
    resolution for actions like ``init`` that never read a profile.
    """
    table_list = " ".join(f"[{namespace}]" for namespace in namespaces)
    execution_text = (
        "Live apply is attempted when the browser is running; plain `apply`\n"
        "manages a local endpoint and normal-close fallback automatically."
    )
    apply_execution_text = execution_text.replace("\n", "\n  ")

    if normalize_args is not None:
        # Propagates onto every subcommand's namespace so cli.main()
        # can run it before dispatch.
        parser.set_defaults(_normalize_args=normalize_args)

    setup_profile_args(parser, leaf=False)

    def leaf_profile_args(action_parser: argparse.ArgumentParser) -> None:
        setup_profile_args(action_parser, leaf=True)

    sub = parser.add_subparsers(dest="module", required=True, metavar="ACTION")

    if cmd_init_fn is not None:
        i = sub.add_parser(
            "init",
            help="scaffold a starter TOML config",
            formatter_class=_HELP_FORMATTER,
            description=f"""\
Write a commented starter config for {display_name}.

The template includes the supported tables: {table_list}.
Without `--output`, the template is printed to stdout. With `--output`,
an existing destination is never overwritten.""",
            epilog="""\
Examples:
  dotbrave init
  dotbrave init -o brave.toml""",
        )
        i.add_argument(
            "-o",
            "--output",
            metavar="FILE",
            help="write to FILE instead of stdout",
        )
        i.set_defaults(func=cmd_init_fn)

    a = sub.add_parser(
        "apply",
        help=f"apply {table_list} tables from a TOML config",
        formatter_class=_HELP_FORMATTER,
        description=f"""\
Apply {table_list} from one TOML document to {display_name}.

Table semantics:
  Missing table   skip that namespace and preserve its managed state.
  Empty table     remove/reset all entries previously managed there.

Safety:
  --dry-run prints the planned diff without writing.
  A real Preferences write creates one timestamped backup and verifies it.
  [settings] keys tracked by Chromium MAC integrity are refused.
  A changed [pwa] table writes managed policy and may require sudo or
  Administrator privileges.

Undo:
  `apply --undo` restores the most recent timestamped Preferences backup
  (each real apply creates one, next to Preferences) and clears dotbrave's
  shortcut/settings sidecars. [pwa] policy and the `export --snapshot`
  baseline are left untouched.

Execution:
  {apply_execution_text}""",
        epilog="""\
The config may be a local path or an HTTPS URL. For remote configs, use
`--expect-sha256` to pin the content. Plain HTTP is refused unless you
explicitly opt in with `--allow-http`.

Examples:
  dotbrave apply --dry-run brave.toml
  dotbrave apply brave.toml
  dotbrave apply --expect-sha256 HEX https://example.com/brave.toml
  dotbrave apply --undo""",
    )
    leaf_profile_args(a)
    a.add_argument(
        "config",
        nargs="?",
        default=None,
        help="path to a local TOML file, or https:// URL to fetch one "
        "(http:// is refused unless --allow-http is set)",
    )
    a.add_argument(
        "--undo",
        action="store_true",
        help="restore the most recent apply-time Preferences backup "
        "instead of applying a config",
    )
    a.add_argument(
        "--expect-sha256",
        metavar="HEX",
        default=None,
        help="when fetching a URL, refuse to apply unless the response sha256 "
        "matches this hex digest",
    )
    a.add_argument(
        "--allow-http",
        action="store_true",
        help="allow fetching configs over plain http:// (NOT recommended; the "
        "response can be modified in transit and a malicious [pwa] table "
        "would run through sudo)",
    )
    a.add_argument(
        "--unattended",
        action="store_true",
        help="never prompt and never close a running Brave. Anything that "
        "would need elevated privileges or a browser restart is reported "
        "on stderr and skipped, and the command still exits 0. Intended "
        "for home-manager activation and other non-interactive runners.",
    )
    a.add_argument(
        "--skip",
        action="append",
        choices=["shortcuts", "settings", "pwa"],
        default=[],
        metavar="NAMESPACE",
        help="do not build a plan for this TOML table; repeatable. Use "
        "when something else owns that namespace -- e.g. `--skip pwa` "
        "when a Nix module writes the managed policy.",
    )
    a.add_argument("-n", "--dry-run", action="store_true")
    a.set_defaults(func=cmd_apply_fn)

    if cmd_export_fn is not None:
        export_scope = (
            "[shortcuts] changes versus Brave defaults, [settings] "
            "well-known keys plus everything dotbrave manages, and [pwa] "
            "force-installed URLs."
        )
        e = sub.add_parser(
            "export",
            help="emit your current customizations as a TOML config",
            formatter_class=_HELP_FORMATTER,
            description=f"""\
Export a round-trippable TOML config for {display_name}.

Output: {export_scope}
The exported file is a valid starter config: save it, edit it, feed it
back to `apply`.

[settings] sources: keys dotbrave already manages, a curated list of
well-known user-facing keys, and -- when you captured a baseline with
`export --snapshot` before changing settings in the browser UI -- any
other key changed since. MAC-protected keys are emitted as comments;
`apply` refuses them. {display_name} persists Preferences on a delay
(~10s): wait a few seconds after a UI change, or quit the browser,
before exporting.

Use `-a` to list every shortcut binding (with command names), not just
customized ones.""",
            epilog="""\
Examples:
  dotbrave export -o brave.toml
  dotbrave export --snapshot     # then change settings in the Brave UI...
  dotbrave export                # ...and see them under [settings]""",
        )
        leaf_profile_args(e)
        e.add_argument(
            "-o",
            "--output",
            metavar="FILE",
            help="write to FILE instead of stdout",
        )
        if export_has_shortcuts:
            e.add_argument(
                "-a",
                "--all-shortcuts",
                action="store_true",
                help="include every shortcut binding, not just user-customized ones",
            )
        e.add_argument(
            "--snapshot",
            action="store_true",
            help="capture a Preferences baseline (instead of exporting); a "
            "later plain `export` lists [settings] keys changed since it",
        )
        e.add_argument(
            "--clear",
            action="store_true",
            help="with --snapshot: delete the stored baseline",
        )
        e.set_defaults(func=cmd_export_fn)

    if cmd_restore_fn is not None:
        r = sub.add_parser(
            "restore",
            help="restore Preferences from a backup created by apply",
            formatter_class=_HELP_FORMATTER,
            description=f"""\
Restore {display_name} Preferences from a backup made by `apply`.

By default, the most recent timestamped backup is selected. A real restore
also clears dotbrave shortcut/settings sidecars so the restored profile
does not remain incorrectly marked as managed.

[pwa] policy is not restored by this command because it is stored outside
Preferences in managed policy storage.""",
            epilog="""\
Examples:
  dotbrave restore --list
  dotbrave restore --dry-run
  dotbrave restore --from FILE""",
        )
        leaf_profile_args(r)
        r.add_argument(
            "--from",
            dest="from_path",
            metavar="FILE",
            default=None,
            help="path to a specific backup file (default: most recent)",
        )
        r.add_argument(
            "--list",
            action="store_true",
            help="list available backups and exit",
        )
        r.add_argument("-n", "--dry-run", action="store_true")
        r.set_defaults(func=cmd_restore_fn)

    for mod_register in module_registers:
        mod_register(sub, leaf_profile_args)
