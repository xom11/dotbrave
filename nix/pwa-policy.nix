# Read [pwa].urls from a brave.toml and build WebAppInstallForceList.
#
# `tomlPath` is a STRING, not a path literal: a path literal would copy the
# file into the store and freeze it there until the next switch, while we
# want to read the working tree. Corollary: eval must run with --impure.
#
# The entry shape must match `_DEFAULT_ENTRY` in src/dotbrave/_base/pwa.py.
{ lib }:
{
  entriesFrom = tomlPath:
    let
      doc = builtins.fromTOML (builtins.readFile tomlPath);
      urls = doc.pwa.urls or [ ];
    in
    map
      (url: {
        inherit url;
        default_launch_container = "window";
        create_desktop_shortcut = true;
      })
      urls;
}
