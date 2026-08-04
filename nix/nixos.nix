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
      example = "/home/you/.nix/home-manager/dotfiles/browser/dotbrave/brave.toml";
      description = ''
        Absolute path to brave.toml, as a string. Only `[pwa].urls` is read,
        and it is read at EVALUATION time -- changing the PWA list needs a
        rebuild, unlike `[shortcuts]`/`[settings]`.

        Reading it uses `builtins.readFile` on an absolute-path string, which
        makes evaluation IMPURE. `nixos-rebuild --flake` is pure by default,
        so every rebuild on a host that enables this module must pass
        `--impure`:

        ```
        nixos-rebuild switch --impure --flake ~/.nix#hostname
        ```

        A path literal would avoid that, but would also copy brave.toml into
        the store and freeze it there -- editing your working tree would then
        have no effect until the next re-copy.

        The file must contain a `[pwa]` table. An absent one is refused --
        this module writes the whole force-list, so it cannot express "leave
        PWAs alone" the way the CLI can. Write `urls = []` if you mean
        "uninstall every PWA", or leave
        {option}`services.dotbrave.enable` off.
      '';
    };
  };

  # The filename must match linux_policy_path in src/dotbrave/pwa.py.
  config = lib.mkIf cfg.enable {
    environment.etc."brave/policies/managed/dotbrave-pwa.json".text =
      builtins.toJSON { WebAppInstallForceList = entries; };
  };
}
