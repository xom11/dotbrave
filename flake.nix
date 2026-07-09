{
  description = "Manage Brave as a dotfile (shortcuts, settings, PWAs)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      mkDotbrave = pkgs: pkgs.python3Packages.buildPythonApplication {
        pname = "dotbrave";
        version = builtins.head
          (builtins.match ''.*__version__ = "([^"]+)".*''
            (builtins.readFile ./src/dotbrave/__init__.py));
        pyproject = true;
        src = ./.;
        build-system = [ pkgs.python3Packages.hatchling ];
        nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
        disabledTests = [
          # touches the real on-disk Brave profile, skipped via env in CI too
          "test_dump_real_profile_succeeds"
          "test_dry_run_apply_real_profile_does_not_write"
        ];
        meta = with pkgs.lib; {
          description = "Manage Brave as a dotfile (shortcuts, settings, PWAs)";
          homepage = "https://github.com/xom11/dotbrave";
          license = licenses.mit;
          mainProgram = "dotbrave";
          platforms = platforms.unix ++ platforms.windows;
        };
      };
    in
    {
      overlays.default = final: _prev: {
        dotbrave = mkDotbrave final;
      };

      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          dotbrave = mkDotbrave pkgs;
        in {
          inherit dotbrave;
          default = dotbrave;
        });

      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            ];
          };
        });
    };
}
