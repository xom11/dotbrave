"""Manage Brave custom keyboard shortcuts via a TOML config file.

Brave stores user-overridden accelerators in its profile `Preferences` JSON
under the `brave.accelerators` key. The values are NOT in the protected
`Secure Preferences` file, so direct patching does not trip MAC integrity
checks (verified against brave/brave-core source).

This module exposes:
- `plan_apply(prefs_path, prefs, raw_table)` — pure: builds a `Plan` from
  a parsed `[shortcuts]` table. Used by the unified `brave apply` runner.
- CLI sub-actions `dump` and `list` (read-only). Apply lives at the
  `brave` level (one entry point for shortcuts + settings together).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotbrave.command_ids import ID_TO_NAME, NAME_TO_ID
from dotbrave.utils import (
    Plan,
    find_preferences,
    get_nested,
    load_prefs,
)

ACCELERATORS_KEY_PATH = ("brave", "accelerators")
DEFAULT_ACCELERATORS_KEY_PATH = ("brave", "default_accelerators")
NAMESPACE = "shortcuts"


def _validate_table(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        sys.exit("error: [shortcuts] must be a table")
    out: dict[str, list[str]] = {}
    for name, value in raw.items():
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            sys.exit(f"error: shortcuts.{name} must be a list of strings")
        out[name] = value
    return out


def _normalize_accelerator(key: str) -> str:
    """Translate the super/cmd modifier to Brave's platform-canonical form.

    Brave serializes the super/cmd key as `Command+` on macOS and `Meta+`
    on Linux/Windows. Writing the wrong spelling is destructive: Brave's
    parser silently drops the unknown modifier on launch, so `Meta+KeyR`
    on macOS reduces to just `KeyR` (a single-letter binding that fires
    while typing). Accept either spelling in the TOML so configs stay
    portable, and rewrite to the current platform's form before persisting.
    """
    if sys.platform == "darwin":
        return key.replace("Meta+", "Command+")
    return key.replace("Command+", "Meta+")


def _normalize_keys(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        norm = _normalize_accelerator(k)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def resolve_command_ids(shortcuts: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    unknown = []
    for name, keys in shortcuts.items():
        if name not in NAME_TO_ID:
            unknown.append(name)
            continue
        out[str(NAME_TO_ID[name])] = keys
    if unknown:
        sys.exit(
            "error: unknown command name(s): "
            + ", ".join(sorted(unknown))
            + "\nrun `dotbrave shortcuts list` to see all known commands"
        )
    return out


def diff_summary(
    current: dict, target: dict, removed_ids: set[str]
) -> list[str]:
    lines = []
    for cid, keys in target.items():
        name = ID_TO_NAME.get(int(cid), f"<unknown:{cid}>")
        if cid not in current:
            lines.append(f"  + {name}: {keys}")
        elif current[cid] != keys:
            lines.append(f"  ~ {name}: {current[cid]} -> {keys}")
    for cid in removed_ids:
        name = ID_TO_NAME.get(int(cid), f"<unknown:{cid}>")
        lines.append(f"  - {name}: {current[cid]} (reset to default)")
    return lines


def _state_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(prefs_path.name + ".dotbrave.shortcuts.json")


def _get_managed_ids(prefs_path: Path) -> set[str]:
    state = _state_file(prefs_path)
    if not state.exists():
        return set()
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("managed_ids", []))


def plan_apply(prefs_path: Path, prefs: dict, raw_table: object) -> Plan:
    """Compute the apply plan for a `[shortcuts]` TOML table.

    Pure: validates input, reads the state file, and returns a `Plan`.
    Does not write anything. Caller (the unified runner) is responsible
    for backups, kill-browser, write_atomic, state-file write, and verify.
    """
    config = _validate_table(raw_table)
    config = {name: _normalize_keys(keys) for name, keys in config.items()}
    target = resolve_command_ids(config)

    current = dict(get_nested(prefs, ACCELERATORS_KEY_PATH))
    defaults = dict(get_nested(prefs, DEFAULT_ACCELERATORS_KEY_PATH))

    target_ids = set(target)
    config_managed_ids = _get_managed_ids(prefs_path)
    removed_ids = {cid for cid in (config_managed_ids - target_ids) if cid in current}

    diff = diff_summary(current, target, removed_ids)

    def apply_fn(prefs: dict) -> None:
        accels = get_nested(prefs, ACCELERATORS_KEY_PATH)
        for cid, keys in target.items():
            accels[cid] = keys
        for cid in removed_ids:
            if cid in defaults:
                accels[cid] = list(defaults[cid])
            else:
                accels.pop(cid, None)

    def verify_fn(reloaded: dict) -> None:
        reloaded_accels = get_nested(reloaded, ACCELERATORS_KEY_PATH)
        for cid, keys in target.items():
            if reloaded_accels.get(cid) != keys:
                sys.exit(f"error: shortcuts verification failed for command {cid}")

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        state_path=_state_file(prefs_path),
        state_payload={"managed_ids": sorted(target_ids, key=int)},
        apply_fn=apply_fn,
        verify_fn=verify_fn,
    )


def build_dump_block(
    prefs: dict,
    *,
    all_bindings: bool = False,
    header_comment: str | None = None,
) -> list[str]:
    """Pure builder for the `[shortcuts]` TOML block.

    Returns the lines as a list (no trailing newline) so callers can
    decide how to join / wrap them.  Used by both ``cmd_dump`` and the
    unified ``<browser> export`` command.
    """
    accels = get_nested(prefs, ACCELERATORS_KEY_PATH)
    defaults = get_nested(prefs, DEFAULT_ACCELERATORS_KEY_PATH)

    lines: list[str] = []
    if header_comment is not None:
        lines.append(header_comment)
    lines.append("[shortcuts]")
    unknown_lines: list[str] = []
    for cid, keys in sorted(accels.items(), key=lambda kv: int(kv[0])):
        if not all_bindings and defaults.get(cid) == keys:
            continue
        name = ID_TO_NAME.get(int(cid))
        keys_repr = "[" + ", ".join(json.dumps(k) for k in keys) + "]"
        if name:
            lines.append(f"{name} = {keys_repr}")
        else:
            unknown_lines.append(f"# unknown command id {cid} = {keys_repr}")
    if unknown_lines:
        lines.append("")
        lines.append("# IDs not recognized by this tool's mapping:")
        lines.extend(unknown_lines)
    return lines


def cmd_dump(args: argparse.Namespace) -> None:
    prefs_path = find_preferences(args.profile_root, args.profile)
    prefs = load_prefs(prefs_path)

    header = "# Generated by `dotbrave shortcuts dump`"
    if not args.all:
        header += " (only entries that differ from defaults; pass --all to see every binding)"
    lines = build_dump_block(prefs, all_bindings=args.all, header_comment=header)
    out = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(out)


def cmd_list(args: argparse.Namespace) -> None:
    needle = (args.filter or "").lower()
    rows = [
        (name, cid)
        for name, cid in sorted(NAME_TO_ID.items())
        if needle in name
    ]
    width = max((len(n) for n, _ in rows), default=0)
    for name, cid in rows:
        print(f"{name:<{width}}  {cid}")
    print(f"\n{len(rows)} commands", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction, profile_args) -> None:
    p = subparsers.add_parser(
        "shortcuts",
        help="inspect keyboard shortcuts (apply lives at `dotbrave apply`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Inspect Brave keyboard shortcut bindings managed through [shortcuts].

Config keys are friendly Brave command names such as `focus_location`.
Binding values use Chromium KeyEvent codes such as `Control+KeyL` or
`Meta+Shift+KeyP`; Meta/Command is normalized for the current OS.""",
        epilog="""\
Examples:
  dotbrave shortcuts list tab
  dotbrave shortcuts dump -o shortcuts.toml
  dotbrave shortcuts dump --all""",
    )
    sub = p.add_subparsers(dest="action", required=True, metavar="ACTION")

    d = sub.add_parser(
        "dump",
        help="emit current shortcuts as TOML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Emit a [shortcuts] TOML table from Brave Preferences.

By default, only bindings differing from Brave defaults are output.
Use `--all` when you need the complete binding map.""",
        epilog="""\
Examples:
  dotbrave shortcuts dump
  dotbrave shortcuts dump --all -o all-shortcuts.toml""",
    )
    profile_args(d)
    d.add_argument("-o", "--output", help="write to file instead of stdout")
    d.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="dump every binding, not just user-customized ones",
    )
    d.set_defaults(func=cmd_dump)

    l = sub.add_parser(
        "list",
        help="list known command names",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
List Brave command names accepted in a [shortcuts] table.

The optional filter is a case-insensitive substring match.""",
        epilog="""\
Examples:
  dotbrave shortcuts list
  dotbrave shortcuts list focus""",
    )
    l.add_argument("filter", nargs="?", help="substring filter")
    l.set_defaults(func=cmd_list)
