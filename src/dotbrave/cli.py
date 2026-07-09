"""Top-level CLI for dotbrave.

Usage:
    dotbrave init -o brave.toml
    dotbrave apply <config>          # writes [shortcuts] + [settings] + [pwa]
    dotbrave export -o snapshot.toml
    dotbrave restore --list
    dotbrave shortcuts dump|list
    dotbrave settings  dump|blocked
    dotbrave pwa       dump

Profile options (--channel, -r/--profile-root, -p/--profile) may be given
before or after the action name; the after-action form wins when both are
present.
"""
from __future__ import annotations

import argparse

from dotbrave import __version__
from dotbrave.browser import register


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotbrave",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Manage Brave browser customizations as TOML dotfiles.

Command shape:
  dotbrave [profile-options] <action> [action-options]

Supported TOML tables: [shortcuts] [settings] [pwa]

`[shortcuts]` manages keyboard shortcuts, `[settings]` writes unprotected
Preferences keys, and `[pwa]` manages force-installed web apps through
browser policy. Live apply is attempted when Brave is running; plain
`apply` manages a local endpoint and normal-close fallback automatically.""",
        epilog="""\
Typical workflow:
  dotbrave init -o brave.toml
  dotbrave apply --dry-run brave.toml
  dotbrave apply brave.toml
  dotbrave export -o snapshot.toml
  dotbrave restore --list

Browser notes:
  `--channel {stable,beta,nightly}` selects the Brave release channel and
  its profile root. Shortcut names map to Brave command IDs; portable
  Meta+ bindings are normalized per OS.

Use `dotbrave <action> --help` for safety details and examples.""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    register(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    normalize = getattr(args, "_normalize_args", None)
    if normalize is not None:
        normalize(args)
    args.func(args)
