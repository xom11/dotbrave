"""Shared settings module logic for all Chromium-based browsers.

All browsers use the same dotted-path Preferences model with
``protection.macs`` MAC integrity checks.  This module implements the
full settings lifecycle; browser-specific modules are thin wrappers
that pass a ``browser_name`` for user-facing strings.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from dotbrave._base.utils import Plan, find_preferences, load_prefs

NAMESPACE = "settings"
_MISSING = object()


def _split_key(dotted: str) -> tuple[str, ...]:
    if not dotted:
        sys.exit("error: empty key in [settings]")
    return tuple(dotted.split("."))


def _get_value(prefs: dict, parts: tuple[str, ...]) -> Any:
    cur: Any = prefs
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _set_value(prefs: dict, parts: tuple[str, ...], value: Any) -> None:
    cur = prefs
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _pop_value(prefs: dict, parts: tuple[str, ...]) -> None:
    cur: Any = prefs
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


_SYNC_KEY_BY_BROWSER: dict[str, tuple[str, ...]] = {
    "brave": ("sync", "has_setup_completed"),
}


def _sync_enabled(browser_name: str, prefs: dict) -> bool:
    """Whether the user has set up sync for this browser.

    The canonical post-setup flag stays true even after the user signs
    out, so the check is conservative (warns slightly more often than
    strictly needed).  An unknown ``browser_name`` falls back to the
    Chromium-style ``sync.has_setup_completed`` key.
    """
    parts = _SYNC_KEY_BY_BROWSER.get(
        browser_name, ("sync", "has_setup_completed")
    )
    cur: object = prefs
    for p in parts:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(p)
    return bool(cur)


def _load_secure_prefs(prefs_path: Path) -> dict:
    """Load the sibling ``Secure Preferences`` file as a dict.

    Chrome stores most tracked-pref entries (and their HMAC integrity
    bookkeeping) in a separate file named ``Secure Preferences`` next
    to ``Preferences``.  Brave/Vivaldi/Edge use the same Chromium prefs
    layout and may also write this file; checking it is conservative
    for every browser in the family.

    Returns ``{}`` if the file is absent, unreadable, or not valid JSON
    -- the caller treats absence of MAC info as "not protected", which
    matches the pre-Secure-Preferences behavior for all callers.
    """
    secure = prefs_path.with_name("Secure Preferences")
    if not secure.exists():
        return {}
    try:
        return json.loads(secure.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _deep_merge_macs(a: dict, b: dict) -> dict:
    """Deep-merge two ``protection.macs`` subtrees.

    Both files store HMACs at the same dotted-path shape, so a key
    present in either file marks that path as tracked.  Values
    themselves are HMAC strings; on the rare conflict (same key in
    both files) ``b`` wins, but the only signal we use downstream is
    presence vs. absence of the leaf, so the choice is immaterial.
    """
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge_macs(out[k], v)
        else:
            out[k] = v
    return out


def _all_macs(prefs: dict, prefs_path: Path) -> dict:
    """Union of MAC bookkeeping from ``Preferences`` and ``Secure Preferences``."""
    main = prefs.get("protection", {}).get("macs", {})
    if not isinstance(main, dict):
        main = {}
    secure_data = _load_secure_prefs(prefs_path)
    secure_macs = secure_data.get("protection", {}).get("macs", {})
    if not isinstance(secure_macs, dict):
        secure_macs = {}
    if not secure_macs:
        return main
    return _deep_merge_macs(main, secure_macs)


def _is_mac_protected(macs: dict, parts: tuple[str, ...]) -> bool:
    cur: Any = macs
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    return True


def _validate_table(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        sys.exit("error: [settings] must be a table")
    return raw


def _state_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(prefs_path.name + ".dotbrave.settings.json")


def _get_managed_keys(prefs_path: Path) -> set[str]:
    state = _state_file(prefs_path)
    if not state.exists():
        return set()
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("managed_keys", []))


def _snapshot_file(prefs_path: Path) -> Path:
    return prefs_path.with_name(
        prefs_path.name + ".dotbrave.settings-snapshot.json"
    )


def _load_snapshot(prefs_path: Path) -> dict | None:
    """Load the export baseline written by `settings snapshot`.

    Returns None when no snapshot exists.  A snapshot the user created
    but that cannot be read is an error, not a silent skip: exporting
    without the baseline would quietly drop the [settings] block.
    """
    snap = _snapshot_file(prefs_path)
    if not snap.exists():
        return None
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(
            f"error: unreadable settings snapshot {snap}: {e}\n"
            "(re-run `dotbrave settings snapshot`, or delete the file)"
        )
    if not isinstance(data, dict) or not isinstance(data.get("prefs"), dict):
        sys.exit(
            f"error: malformed settings snapshot {snap}\n"
            "(re-run `dotbrave settings snapshot`, or delete the file)"
        )
    data.setdefault("created", "?")
    if not isinstance(data.get("secure_prefs"), dict):
        data["secure_prefs"] = {}
    return data


def cmd_snapshot(browser_name: str, args: argparse.Namespace) -> None:
    prefs_path = find_preferences(args.profile_root, args.profile)
    snap = _snapshot_file(prefs_path)

    if getattr(args, "clear", False):
        if snap.exists():
            snap.unlink()
            print(f"removed {snap}")
        else:
            print(f"no snapshot to remove at {snap}")
        return

    prefs = load_prefs(prefs_path)
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "created": created,
        "prefs": prefs,
        "secure_prefs": _load_secure_prefs(prefs_path),
    }
    snap.write_text(json.dumps(payload), encoding="utf-8")
    print(f"snapshot saved: {snap} ({created})")
    print(
        f"Change settings in the {browser_name.title()} UI, then run "
        "`dotbrave export` -- its [settings] block will list what changed."
    )


def diff_summary(
    prefs: dict,
    target: dict[str, Any],
    removed_keys: set[str],
) -> list[str]:
    lines = []
    for key in sorted(target):
        parts = _split_key(key)
        cur = _get_value(prefs, parts)
        new = target[key]
        if cur is _MISSING:
            lines.append(f"  + {key} = {json.dumps(new)}")
        elif cur != new:
            lines.append(f"  ~ {key}: {json.dumps(cur)} -> {json.dumps(new)}")
    for key in sorted(removed_keys):
        parts = _split_key(key)
        cur = _get_value(prefs, parts)
        if cur is _MISSING:
            continue
        lines.append(f"  - {key}: {json.dumps(cur)} (removed)")
    return lines


def plan_apply(browser_name: str, prefs_path: Path, prefs: dict, raw_table: object) -> Plan:
    target = _validate_table(raw_table)

    macs = _all_macs(prefs, prefs_path)
    rejected: list[str] = []
    for key in target:
        parts = _split_key(key)
        if parts[0] == "protection":
            rejected.append(f"{key} (Chromium MAC bookkeeping subtree)")
            continue
        if _is_mac_protected(macs, parts):
            rejected.append(f"{key} (MAC-protected; writing would be reset on launch)")
    if rejected:
        sys.exit(
            "error: the following [settings] keys cannot be written in v1:\n  "
            + "\n  ".join(rejected)
            + "\n(remove them from your config; MAC support is planned for v2)"
        )

    target_keys = set(target)
    config_managed_keys = _get_managed_keys(prefs_path)
    removed_keys = config_managed_keys - target_keys

    diff = diff_summary(prefs, target, removed_keys)

    warnings: list[str] = []
    if (target or removed_keys) and _sync_enabled(browser_name, prefs):
        warnings.append(
            f"warning: {browser_name.title()} Sync is enabled "
            f"(sync.has_setup_completed=true).\n"
            f"  Synced settings can be overwritten on Sync's next pulse "
            f"from another device.\n"
            f"  Most keys dotbrave writes are local-only -- the commonly "
            f"synced ones (homepage,\n"
            f"  default search, startup URLs) are MAC-protected and already "
            f"refused.  Proceeding."
        )

    def apply_fn(prefs: dict) -> None:
        for key, value in target.items():
            _set_value(prefs, _split_key(key), value)
        for key in removed_keys:
            _pop_value(prefs, _split_key(key))

    def verify_fn(reloaded: dict) -> None:
        for key, value in target.items():
            got = _get_value(reloaded, _split_key(key))
            if got != value:
                sys.exit(f"error: settings verification failed for key {key!r}: got {got!r}")

    return Plan(
        namespace=NAMESPACE,
        diff_lines=diff,
        state_path=_state_file(prefs_path),
        state_payload={"managed_keys": sorted(target_keys)},
        apply_fn=apply_fn,
        verify_fn=verify_fn,
        warnings=warnings,
    )


def _format_toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_format_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        items = ", ".join(
            f"{json.dumps(k)} = {_format_toml_value(val)}" for k, val in v.items()
        )
        return "{" + items + "}"
    raise ValueError(f"unsupported value type for TOML emission: {type(v).__name__}")


def _walk_mac_leaves(
    macs: Any, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Collect every leaf path inside ``protection.macs``.

    Each leaf is a tracked pref's MAC entry; the corresponding pref
    path (same dotted-path under the profile root) is what ``apply``
    refuses.  Parent dicts in ``protection.macs`` are skipped because
    their children carry the user-facing names.
    """
    if not isinstance(macs, dict):
        return [prefix] if prefix else []
    out: list[tuple[str, ...]] = []
    for k, v in macs.items():
        out.extend(_walk_mac_leaves(v, prefix + (k,)))
    return out


def cmd_blocked(browser_name: str, args: argparse.Namespace) -> None:
    prefs_path = find_preferences(args.profile_root, args.profile)
    prefs = load_prefs(prefs_path)

    macs = _all_macs(prefs, prefs_path)
    paths = sorted(_walk_mac_leaves(macs))
    secure_prefs = _load_secure_prefs(prefs_path)

    title = browser_name.title()
    lines = [
        "# Generated by `dotbrave settings blocked`",
        "# MAC-protected keys in this profile -- `dotbrave apply`",
        f"# will refuse to write these.  Set them via the {title} UI instead.",
        "[settings]",
    ]
    if not paths:
        lines.append("# (no MAC-protected keys present in this profile)")
    else:
        for parts in paths:
            key = ".".join(parts)
            cur = _get_value(prefs, parts)
            if cur is _MISSING and secure_prefs:
                cur = _get_value(secure_prefs, parts)
            if cur is _MISSING:
                lines.append(f"# {json.dumps(key)} = ?  (not set in Preferences)")
                continue
            try:
                rhs = _format_toml_value(cur)
            except ValueError:
                rhs = json.dumps(cur)
            lines.append(f"# {json.dumps(key)} = {rhs}")

    out = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(out)


def cmd_dump(browser_name: str, args: argparse.Namespace) -> None:
    prefs_path = find_preferences(args.profile_root, args.profile)
    prefs = load_prefs(prefs_path)

    if args.keys:
        keys = list(args.keys)
    else:
        keys = sorted(_get_managed_keys(prefs_path))
        if not keys:
            sys.exit(
                "error: no managed keys to dump (state file is empty).\n"
                f"Pass keys explicitly: `dump {browser_name}.tabs.vertical_tabs_enabled ...`"
            )

    lines = ["# Generated by `dotbrave settings dump`", "[settings]"]
    missing: list[str] = []
    for key in keys:
        val = _get_value(prefs, _split_key(key))
        if val is _MISSING:
            missing.append(key)
            continue
        lines.append(f"{json.dumps(key)} = {_format_toml_value(val)}")
    if missing:
        lines.append("")
        lines.append("# keys not present in Preferences:")
        for k in missing:
            lines.append(f"#   {k}")
    out = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(out)


def register(
    browser_name: str,
    subparsers: argparse._SubParsersAction,
    profile_args,
) -> argparse._SubParsersAction:
    """Register the ``settings`` subcommand and return its action sub-parser.

    ``profile_args`` attaches the profile flags to each leaf action so
    they may be given after the action name. The returned object is the
    inner ``add_subparsers()`` action, kept for parity with the other
    module ``register()`` hooks.
    """
    p = subparsers.add_parser(
        "settings",
        help="inspect general settings (apply lives at `dotbrave apply`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Inspect general {browser_name.title()} Preferences managed through [settings].

`dump` prints currently managed keys by default, or explicitly requested
dotted paths. `blocked` lists MAC-protected Preferences keys that
`dotbrave apply` refuses rather than writing values the
browser would reset on launch. `snapshot` captures a baseline so
`dotbrave export` can emit [settings] keys changed via the browser UI.""",
        epilog="""\
Examples:
  dotbrave settings dump
  dotbrave settings dump bookmark_bar.show_on_all_tabs
  dotbrave settings blocked
  dotbrave settings snapshot""",
    )
    sub = p.add_subparsers(dest="action", required=True, metavar="ACTION")

    d = sub.add_parser(
        "dump",
        help="emit current values as TOML -- managed keys (default) or specific keys",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Emit a [settings] TOML block from {browser_name.title()} Preferences.

Without keys, output is limited to keys tracked by a prior dotbrave
apply. Pass dotted keys explicitly to inspect values not yet managed.""",
        epilog="""\
Examples:
  dotbrave settings dump
  dotbrave settings dump bookmark_bar.show_on_all_tabs -o settings.toml""",
    )
    profile_args(d)
    d.add_argument(
        "keys",
        nargs="*",
        help="dotted-path keys to dump; defaults to currently-managed keys",
    )
    d.add_argument("-o", "--output", help="write to file instead of stdout")
    d.set_defaults(func=lambda args, bn=browser_name: cmd_dump(bn, args))

    b = sub.add_parser(
        "blocked",
        help="list MAC-protected keys that `apply` will refuse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Print MAC-protected preference paths for the selected {browser_name.title()} profile.

These keys cannot safely be written by dotbrave: Chromium verifies their
integrity at startup and would reset unauthenticated changes.""",
        epilog="""\
Example:
  dotbrave settings blocked -o blocked-settings.toml""",
    )
    profile_args(b)
    b.add_argument("-o", "--output", help="write to file instead of stdout")
    b.set_defaults(func=lambda args, bn=browser_name: cmd_blocked(bn, args))

    s = sub.add_parser(
        "snapshot",
        help="capture a Preferences baseline so `export` can diff [settings]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"""\
Capture a baseline snapshot of the selected profile's Preferences (and
Secure Preferences) for `dotbrave export`.

Workflow: run `snapshot`, change settings in the {browser_name.title()} UI,
then run `dotbrave export` -- its [settings] block lists keys that changed
since the snapshot.  The snapshot is kept until overwritten by the next
`snapshot` or deleted with `--clear`; `export` never consumes it.

{browser_name.title()} persists Preferences on a delay (~10s): wait a few
seconds after changing a setting, or quit the browser, before exporting.""",
        epilog="""\
Examples:
  dotbrave settings snapshot
  dotbrave settings snapshot --clear""",
    )
    profile_args(s)
    s.add_argument(
        "--clear", action="store_true", help="delete the stored snapshot"
    )
    s.set_defaults(func=lambda args, bn=browser_name: cmd_snapshot(bn, args))

    return sub
