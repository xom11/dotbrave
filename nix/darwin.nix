# [pwa] on macOS. Harder than Linux because macOS prunes plists it does not
# own from /Library/Managed Preferences during boot, so the daemon races
# Brave's own login item: a Brave that starts first reads an empty policy and
# uninstalls every PWA. ThrottleInterval is pinned to 1 (launchd's default of
# 10 seconds is enough of a delay to lose that race) and StartInterval
# re-checks periodically in case a WatchPaths notification is dropped. The
# `cmp -s` guard inside heal.sh keeps both triggers cheap and loop-free.
#
# Two consequences of building that plist at EVALUATION time, both documented
# on the options below because they are traps rather than details:
#   - it is written whole, so this module OWNS the file: any other Brave policy
#     key an MDM profile put there is dropped. The CLI merges; Nix cannot,
#     because the existing plist is runtime state.
#   - there is no teardown hook, so disabling the module leaves the file behind
#     and still schg-pinned. Removing it is a manual, documented two-liner.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.dotbrave;
  entries = (import ./pwa-policy.nix { inherit lib; }).entriesFrom cfg.config;

  bundle = "com.brave.Browser";
  label = "org.dotbrave.${bundle}.pwa";
  managedPlist = "/Library/Managed Preferences/${bundle}.plist";
  healLog = "/Library/Application Support/dotbrave/${bundle}.heal.log";

  # The source of truth lives in the store: root-owned and read-only. Exactly
  # the property _sudo_install_file works for with chown/chmod, but free.
  sourcePlist = pkgs.writeText "${bundle}.managed.plist"
    (lib.generators.toPlist { escape = true; }
      { WebAppInstallForceList = entries; });

  healer = pkgs.writeShellScript "dotbrave-${bundle}-heal" ''
    export SRC=${sourcePlist}
    export DEST=${lib.escapeShellArg managedPlist}
    export LOG=${lib.escapeShellArg healLog}
    export BUNDLE=${lib.escapeShellArg bundle}
    exec /bin/sh ${cfg.package}/share/dotbrave/heal.sh
  '';
in
{
  options.services.dotbrave = {
    enable = lib.mkEnableOption "dotbrave-managed Brave PWA policy" // {
      description = ''
        Manage Brave's `WebAppInstallForceList` from `[pwa].urls` in
        {option}`services.dotbrave.config`.

        ::: {.warning}
        This module takes **exclusive ownership** of
        `/Library/Managed Preferences/com.brave.Browser.plist`. It writes that
        file from scratch with `WebAppInstallForceList` as its ONLY key, then
        pins it `schg` and re-asserts it every 60 seconds. Any other Brave
        policy key already in that plist -- `HomepageLocation`,
        `ExtensionInstallForcelist`, proxy settings, anything an MDM profile
        put there -- is DROPPED and stays dropped.

        The dotbrave CLI does not behave this way: it reads the existing
        payload and merges, replacing only `WebAppInstallForceList`. The Nix
        module cannot, because the existing plist is runtime state and this
        file is built at evaluation time.

        Do not enable this on a Mac managed by an MDM profile that sets other
        `com.brave.Browser` policy keys. Use `dotbrave apply` (which merges)
        or your MDM instead.
        :::

        ::: {.note}
        There is no automatic teardown. Setting this back to `false` removes
        the LaunchDaemon but leaves the managed plist on disk, still `schg`
        (immutable) -- so even `sudo rm` fails until the flag is lifted. Undo
        it by hand:

        ```
        sudo chflags noschg "/Library/Managed Preferences/com.brave.Browser.plist"
        sudo rm "/Library/Managed Preferences/com.brave.Browser.plist"
        ```

        Do that BEFORE, or right after, the rebuild that disables the module;
        with the daemon gone nothing else will ever touch that file. (Brave
        keeps enforcing the stale force-list until it is removed.)
        :::
      '';
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.dotbrave;
      defaultText = lib.literalExpression "pkgs.dotbrave";
      description = "The dotbrave package to use.";
    };
    config = lib.mkOption {
      type = lib.types.str;
      example = "/Users/you/.nix/home-manager/dotfiles/browser/dotbrave/brave.toml";
      description = ''
        Absolute path to brave.toml, as a string. Only `[pwa].urls` is read,
        and it is read at EVALUATION time -- changing the PWA list needs a
        rebuild, unlike `[shortcuts]`/`[settings]`.

        Reading it uses `builtins.readFile` on an absolute-path string, which
        makes evaluation IMPURE. `darwin-rebuild --flake` is pure by default,
        so every rebuild on a host that enables this module must pass
        `--impure`:

        ```
        darwin-rebuild switch --impure --flake ~/.nix#hostname
        ```

        A path literal would avoid that, but would also copy brave.toml into
        the store and freeze it there -- editing your working tree would then
        have no effect until the next `nix flake update`-style re-copy.

        The file must contain a `[pwa]` table. An absent one is refused (see
        {option}`services.dotbrave.enable`); write `urls = []` if you mean
        "uninstall every PWA".
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    launchd.daemons.${label} = {
      serviceConfig = {
        Label = label;
        ProgramArguments = [ "${healer}" ];
        WatchPaths = [ "/Library/Managed Preferences" ];
        RunAtLoad = true;
        ThrottleInterval = 1;
        StartInterval = 60;
      };
    };
  };
}
