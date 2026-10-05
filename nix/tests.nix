# Eval-time tests for nix/pwa-policy.nix.
#
# The Nix path is the ONLY validation [pwa] gets on a machine set up the
# intended way (the home-manager module passes `--skip pwa`, so the CLI never
# builds a [pwa] plan), which makes its edge cases worth pinning down here
# rather than discovering them on a rebuild.
#
# Run:
#   nix build .#checks.<system>.pwa-policy -L
#
# `entriesFrom` takes a path STRING and readFile's it, so each case
# materializes its TOML with `builtins.toFile`. That is a store path, so these
# tests are pure -- unlike the real call sites, which point at a working tree
# and therefore need --impure.
{ lib }:
let
  policy = import ./pwa-policy.nix { inherit lib; };

  # `builtins.tryEval` catches `throw`, which is what pwa-policy.nix raises;
  # `deepSeq` is needed because the result is a lazy list whose elements would
  # otherwise never be forced.
  evalFails = expr: !(builtins.tryEval (builtins.deepSeq expr expr)).success;

  entriesOf = name: text: policy.entriesFrom (builtins.toFile name text);

  entry = url: {
    inherit url;
    default_launch_container = "window";
    create_desktop_shortcut = true;
  };

  cases = {
    # An ABSENT [pwa] table is not "uninstall everything". The CLI reads it as
    # "do not manage PWAs at all", and a module that installs an empty
    # force-list instead would silently uninstall every PWA on the machine.
    absent-pwa-table-is-refused = evalFails (entriesOf "no-pwa.toml" ''
      [shortcuts]
      new_tab = [ "Control+KeyT" ]
    '');

    # An EXPLICITLY empty list is the deliberate "uninstall all" -- it must
    # keep working, and must not be caught by the check above.
    explicit-empty-urls-yields-empty-list =
      entriesOf "empty-urls.toml" ''
        [pwa]
        urls = []
      '' == [ ];

    # `[pwa]` with no `urls` key at all is the same explicit gesture, and
    # validate_table() in _base/pwa.py reads it the same way (`raw.get("urls",
    # [])`). Present-but-bare stays "uninstall all"; only absent is refused.
    bare-pwa-table-yields-empty-list = entriesOf "bare-pwa.toml" ''
      [pwa]
    '' == [ ];

    urls-become-force-list-entries = entriesOf "two.toml" ''
      [pwa]
      urls = [ "https://a.example/", "https://b.example/" ]
    '' == [ (entry "https://a.example/") (entry "https://b.example/") ];

    duplicate-urls-are-dropped = entriesOf "dup.toml" ''
      [pwa]
      urls = [ "https://a.example/", "https://a.example/" ]
    '' == [ (entry "https://a.example/") ];

    non-https-url-is-refused = evalFails (entriesOf "http.toml" ''
      [pwa]
      urls = [ "http://a.example/" ]
    '');

    unsupported-pwa-key-is-refused = evalFails (entriesOf "extra.toml" ''
      [pwa]
      urls = []
      nonsense = true
    '');

    non-string-url-is-refused = evalFails (entriesOf "int.toml" ''
      [pwa]
      urls = [ 42 ]
    '');

    # `name` is the placeholder name Brave uses when it cannot read a manifest.
    named-entry-sets-fallback-app-name = entriesOf "named.toml" ''
      [pwa]
      urls = [ "https://a.example/", { url = "https://b.example/", name = "B" } ]
    '' == [
      (entry "https://a.example/")
      (entry "https://b.example/" // { fallback_app_name = "B"; })
    ];

    duplicate-url-keeps-the-first-entry = entriesOf "dup-named.toml" ''
      [pwa]
      urls = [ { url = "https://a.example/", name = "A" }, "https://a.example/" ]
    '' == [ (entry "https://a.example/" // { fallback_app_name = "A"; }) ];

    unknown-entry-key-is-refused = evalFails (entriesOf "entry-extra.toml" ''
      [pwa]
      urls = [ { url = "https://a.example/", icon = "x.png" } ]
    '');

    entry-without-url-is-refused = evalFails (entriesOf "entry-no-url.toml" ''
      [pwa]
      urls = [ { name = "A" } ]
    '');

    blank-name-is-refused = evalFails (entriesOf "entry-blank.toml" ''
      [pwa]
      urls = [ { url = "https://a.example/", name = " " } ]
    '');
  };

  failed = lib.filter (name: !cases.${name}) (lib.attrNames cases);
in
lib.throwIf (failed != [ ])
  ("nix/tests.nix: FAILED: " + lib.concatStringsSep ", " failed)
  cases
