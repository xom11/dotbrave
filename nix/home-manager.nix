# [shortcuts] + [settings] at home.activation. Needs no privileges.
# [pwa] is deliberately skipped: nixosModules/darwinModules write it at the
# system level, where the rebuild already runs as root.
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.dotbrave;
in
{
  options.programs.dotbrave = {
    enable = lib.mkEnableOption "dotbrave-managed Brave shortcuts and settings";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.dotbrave;
      defaultText = lib.literalExpression "pkgs.dotbrave";
      description = "The dotbrave package to use.";
    };

    config = lib.mkOption {
      type = lib.types.str;
      example = "/home/you/.nix/home-manager/dotfiles/browser/dotbrave/brave.toml";
      description = ''
        Absolute path to brave.toml, as a string.

        A string (not a path literal) so the file is read from your working
        tree at activation time -- editing it takes effect on the next
        activation with no rebuild.
      '';
    };

    skip = lib.mkOption {
      type = lib.types.listOf (lib.types.enum [ "shortcuts" "settings" "pwa" ]);
      default = [ "pwa" ];
      description = ''
        Namespaces the CLI must not touch. Defaults to [ "pwa" ] because
        services.dotbrave (NixOS/darwin) owns the managed policy.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    home.activation.dotbrave = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${lib.getExe cfg.package} apply --unattended \
        ${lib.concatMapStringsSep " " (n: "--skip ${n}") cfg.skip} \
        ${lib.escapeShellArg cfg.config} || \
        echo "dotbrave: apply failed, continuing activation" >&2
    '';
  };
}
