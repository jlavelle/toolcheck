{
  description = "A linter for LLM tool calls";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
    in
    {
      packages = forAllSystems (pkgs: {
        default = self.packages.${pkgs.stdenv.hostPlatform.system}.toolcheck;
        toolcheck = pkgs.python3Packages.buildPythonApplication {
          pname = project.name;
          inherit (project) version;
          pyproject = true;
          src = nixpkgs.lib.fileset.toSource {
            root = ./.;
            fileset = nixpkgs.lib.fileset.unions [
              ./pyproject.toml
              ./README.md
              ./src
              ./tests
              ./examples
            ];
          };
          build-system = [ pkgs.python3Packages.hatchling ];
          nativeCheckInputs = [
            pkgs.python3Packages.pytestCheckHook
            pkgs.python3Packages.hypothesis
          ];
          # These run the CLI through `uv run`, which needs the network.
          disabledTestPaths = [
            "tests/test_claude_hook.py"
            "tests/test_pi_hook.py"
          ];
          preCheck = ''
            export HOME=$(mktemp -d)
          '';
          meta = {
            inherit (project) description;
            license = nixpkgs.lib.licenses.mit;
            mainProgram = "toolcheck";
          };
        };
      });

      checks = forAllSystems (pkgs: {
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) toolcheck;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python3
            pkgs.uv
            pkgs.just
            pkgs.bun
            pkgs.nodejs
            pkgs.ruff
          ];
          env = {
            UV_PYTHON = "${pkgs.python3}/bin/python3";
            UV_PYTHON_DOWNLOADS = "never";
          };
        };
      });
    };
}
