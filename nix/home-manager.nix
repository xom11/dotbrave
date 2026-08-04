# [shortcuts] + [settings] at home.activation. Needs no privileges.
# [pwa] is deliberately skipped: nixosModules/darwinModules write it at the
# system level, where the rebuild already runs as root.
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.dotbrave;

  # dotbrave shells out to the OS process-listing tool to decide whether Brave
  # is running: `pgrep` on POSIX, `tasklist` on Windows. home-manager's
  # activation script exports a PATH of Nix store paths ONLY -- no /usr/bin,
  # no /bin -- and on darwin `pgrep` lives in /usr/bin. Without this, dotbrave
  # cannot see a running Brave; it then writes Preferences offline and the
  # live Brave flushes its in-memory copy back over the write.
  #
  # These are PATH *directories*, not hardcoded binary paths: dotbrave still
  # calls `pgrep` by bare name and still gets whatever the machine provides.
  # On darwin that has to be the OS copy (nixpkgs has no darwin procps, and
  # macOS ships pgrep in /usr/bin on every machine); on Linux it comes from
  # nixpkgs, so nothing outside the store is assumed.
  processToolsPath =
    if pkgs.stdenv.hostPlatform.isDarwin
    then "/usr/bin:/bin"
    else lib.makeBinPath [ pkgs.procps ];
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

        This module itself does not read the file at evaluation time, so it
        alone does not make evaluation impure. Its companions do:
        {option}`services.dotbrave.config` (NixOS/darwin) reads `[pwa].urls`
        with `builtins.readFile` on this same absolute-path string, so any
        host that enables both must rebuild with `--impure`:

        ```
        nixos-rebuild switch --impure --flake ~/.nix#hostname
        ```
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

    # PATH is prepended, not replaced, and the whole thing runs in a subshell
    # so no later activation entry inherits the change.
    home.activation.dotbrave = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      (
        export PATH="${processToolsPath}:$PATH"
        run ${lib.getExe cfg.package} apply --unattended \
          ${lib.concatMapStringsSep " " (n: "--skip ${n}") cfg.skip} \
          ${lib.escapeShellArg cfg.config}
      ) || echo "dotbrave: apply failed, continuing activation" >&2
    '';
  };
}
