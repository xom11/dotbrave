# [pwa] on macOS. Harder than Linux because macOS prunes plists it does not
# own from /Library/Managed Preferences during boot, so the daemon races
# Brave's own login item: a Brave that starts first reads an empty policy and
# uninstalls every PWA. ThrottleInterval is pinned to 1 (launchd's default of
# 10 seconds is enough of a delay to lose that race) and StartInterval
# re-checks periodically in case a WatchPaths notification is dropped. The
# `cmp -s` guard inside heal.sh keeps both triggers cheap and loop-free.
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
    enable = lib.mkEnableOption "dotbrave-managed Brave PWA policy";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.dotbrave;
      defaultText = lib.literalExpression "pkgs.dotbrave";
      description = "The dotbrave package to use.";
    };
    config = lib.mkOption {
      type = lib.types.str;
      description = ''
        Absolute path to brave.toml, as a string. Only [pwa].urls is read,
        at EVALUATION time -- changing the PWA list needs a rebuild.
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
