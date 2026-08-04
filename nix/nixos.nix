# [pwa] on Linux: a single JSON file under /etc. nixos-rebuild already runs
# as root, so there is no prompt anywhere.
{ config, lib, ... }:
let
  cfg = config.services.dotbrave;
  entries = (import ./pwa-policy.nix { inherit lib; }).entriesFrom cfg.config;
in
{
  options.services.dotbrave = {
    enable = lib.mkEnableOption "dotbrave-managed Brave PWA policy";
    config = lib.mkOption {
      type = lib.types.str;
      description = ''
        Absolute path to brave.toml, as a string. Only [pwa].urls is read,
        and it is read at EVALUATION time -- changing the PWA list needs a
        rebuild, unlike [shortcuts]/[settings].
      '';
    };
  };

  # The filename must match linux_policy_path in src/dotbrave/pwa.py.
  config = lib.mkIf cfg.enable {
    environment.etc."brave/policies/managed/dotbrave-pwa.json".text =
      builtins.toJSON { WebAppInstallForceList = entries; };
  };
}
