"""Top-level CLI for dotbrave.

Usage:
    dotbrave export -o brave.toml    # read current customizations as TOML
    dotbrave apply brave.toml        # write [shortcuts] + [settings] + [pwa]
    dotbrave apply --undo            # revert the last apply

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

Two actions:
  export   read your current customizations as a TOML config
  apply    write a TOML config back to Brave (or `--undo` the last apply)

Supported TOML tables: [shortcuts] [settings] [pwa]

`[shortcuts]` manages keyboard shortcuts, `[settings]` writes unprotected
Preferences keys, and `[pwa]` manages force-installed web apps through
browser policy. Live apply is attempted when Brave is running; plain
`apply` manages a local endpoint and normal-close fallback automatically.""",
        epilog="""\
Typical workflow:
  dotbrave export -o brave.toml    # start from your current state
  (edit brave.toml)
  dotbrave apply --dry-run brave.toml
  dotbrave apply brave.toml
  dotbrave apply --undo            # roll back if needed

Browser notes:
  `--channel {stable,beta,nightly}` selects the Brave release channel and
  its profile root. Shortcut names map to Brave command IDs (see them all
  with `export -a`); portable Meta+ bindings are normalized per OS.

Use `dotbrave export --help` / `dotbrave apply --help` for safety details
and examples.""",
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
