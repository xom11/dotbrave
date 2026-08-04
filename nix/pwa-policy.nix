# Read [pwa].urls from a brave.toml and build WebAppInstallForceList.
#
# `tomlPath` is a STRING, not a path literal: a path literal would copy the
# file into the store and freeze it there until the next switch, while we
# want to read the working tree. Corollary: eval must run with --impure.
#
# The entry shape must match `_DEFAULT_ENTRY` in src/dotbrave/_base/pwa.py.
{ lib }:
let
  # Mirrors validate_table() in src/dotbrave/_base/pwa.py, and has to: the CLI
  # only validates [pwa] while building a [pwa] plan, and the whole point of
  # this split is that the home-manager module passes `--skip pwa`. On a
  # machine set up the intended way that plan is never built, so this is the
  # only validation [pwa] gets. Everything below is a hard eval failure rather
  # than a filter -- a policy that quietly dropped a malformed URL would look
  # exactly like one that worked.
  validUrls = tomlPath: doc:
    let
      at = "dotbrave: ${tomlPath}: [pwa]";

      # An ABSENT [pwa] table and an EMPTY one are opposites, and defaulting
      # the missing table to `{ }` would quietly collapse them into the
      # destructive one. The CLI reads a missing table as "this namespace has
      # another owner, leave it alone"; but this module can only ever write a
      # COMPLETE force-list, so the same input here would install an empty
      # policy -- and Brave uninstalls every PWA not named in the list it is
      # given. There is no way to express "leave it alone" in a file whose
      # whole content is the policy, so refuse instead of guessing.
      raw = doc.pwa or (throw ''
        ${at} table is missing, but services.dotbrave.enable = true.
          This module writes the WHOLE force-list, so an absent table cannot
          mean "leave PWAs alone" the way it does for the dotbrave CLI -- it
          would install an empty policy and Brave would uninstall every PWA.
          Either add a [pwa] table to ${tomlPath} (use `urls = []` if you
          really do mean "uninstall all"), or set
          services.dotbrave.enable = false.'');

      extra = builtins.attrNames (builtins.removeAttrs raw [ "urls" ]);
      # A present-but-bare `[pwa]` keeps meaning "uninstall all", matching
      # `raw.get("urls", [])` in validate_table(). Only ABSENT is refused.
      urls = raw.urls or [ ];

      checkUrl = u:
        if !builtins.isString u then
          throw ("${at} url entries must be strings, got ${builtins.typeOf u}: "
            + lib.generators.toPretty { multiline = false; } u)
        else if !lib.hasPrefix "https://" u then
          throw ''${at} invalid url "${u}" (must start with https://)''
        else
          u;
    in
    lib.throwIf (!builtins.isAttrs raw)
      "${at} must be a table"
      (lib.throwIf (extra != [ ])
        ("${at} has unsupported keys: ${lib.concatStringsSep ", " extra}. "
          + "v1 only supports `urls = [...]`")
        (lib.throwIf (!builtins.isList urls)
          "${at} urls must be an array of strings"
          # Duplicates are dropped rather than rejected, matching Python.
          (lib.unique (map checkUrl urls))));
in
{
  entriesFrom = tomlPath:
    map
      (url: {
        inherit url;
        default_launch_container = "window";
        create_desktop_shortcut = true;
      })
      (validUrls tomlPath (builtins.fromTOML (builtins.readFile tomlPath)));
}
