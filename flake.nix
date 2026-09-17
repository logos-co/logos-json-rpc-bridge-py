{
  description = "logos-bridge: Python client for logos-json-rpc-bridge";

  nixConfig = {
    extra-substituters = [ "https://cache.nix.logos.co/public" ];
    extra-trusted-public-keys = [ "public:l4HrXgL4nw246+LBh2SOJyhz64BoGegOYLheT/iIAPU=" ];
  };

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";
    # The bridge owns the contract reader (lidl) and package tool (lgx) pins, so the
    # codegen reads contracts with the logos-lidl the bridge serves them with.
    logos-json-rpc-bridge.url = "github:logos-co/logos-json-rpc-bridge";
    logos-lidl.follows = "logos-json-rpc-bridge/logos-lidl";
    logos-package.follows = "logos-json-rpc-bridge/logos-package";
    # Providers built with a module-builder that derives lidl(), and the daemon they run under.
    logos-test-modules.url = "github:logos-co/logos-test-modules";
    logos-test-modules.inputs.logos-nix.follows = "logos-nix";
    logos-logoscore-cli.follows = "logos-test-modules/logos-logoscore-cli";
    logos-logoscore-py = { url = "github:logos-co/logos-logoscore-py"; flake = false; };
  };

  outputs = { self, nixpkgs, logos-json-rpc-bridge, logos-lidl, logos-package, logos-test-modules,
              logos-logoscore-cli, logos-logoscore-py, ... }:
    let
      lib = nixpkgs.lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      version = "0.1.0";

      # Only what the package and its tests read: no result links, caches or bytecode.
      src = let fs = lib.fileset; in fs.toSource {
        root = ./.;
        fileset = fs.difference
          (fs.unions [ ./src ./tests ./scripts ./pyproject.toml ./README.md ./LICENSE-MIT ./LICENSE-APACHE-v2 ])
          (fs.unions [
            (fs.fileFilter (file: file.hasExt "pyc") ./src)
            (fs.fileFilter (file: file.hasExt "pyc") ./tests)
          ]);
      };

      # logos-lidl ships lidl-cli from 2043d8b (logos-lidl#14) on. With an older reader the unit
      # checks skip their CLI tests; the checks and apps that need it stop here.
      lidlCliOrNull = pkgs: logos-lidl.packages.${pkgs.stdenv.hostPlatform.system}.lidl-cli or null;
      lidlExe = pkgs:
        let cli = lidlCliOrNull pkgs; in
        if cli != null then "${cli}/bin/lidl" else throw ''
          logos-bridge: this output needs the lidl CLI, and the logos-lidl the bridge pins has no
          packages.${pkgs.stdenv.hostPlatform.system}.lidl-cli (logos-lidl has it from 2043d8b on).
          Point the bridge's reader, which ours follows, at one that has it:
          LOGOS_DEV_LIDL=github:logos-co/logos-lidl scripts/dev-overrides nix ... '';
      # What `lidl --version` prints for the locked input (the CLI embeds the same revision).
      lidlRev = logos-lidl.shortRev or logos-lidl.dirtyShortRev or "unknown";
      lgxExe = pkgs: "${logos-package.packages.${pkgs.stdenv.hostPlatform.system}.lgx}/bin/lgx";

      cliEnv = pkgs:
        { LOGOS_LGX_CLI = lgxExe pkgs; }
        // lib.optionalAttrs (lidlCliOrNull pkgs != null) { LOGOS_LIDL_CLI = lidlExe pkgs; };

      # The integration stack: what logos_bridge.testing.live reads (the daemon, the bridge's and
      # the providers' installed modules), and what tests/integration/harness.py adds (each
      # provider's #lidl output and package, and the tools).
      providers = [ "test_fullapi_cpp" "test_fullapi_ext_cpp" ];
      stackEnv = pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
          modules = logos-test-modules.modules.${system};
          bridge = logos-json-rpc-bridge.packages.${system};
          providersDir = pkgs.runCommand "logos-bridge-providers" { } ''
            mkdir -p $out
            ${lib.concatMapStrings (m: ''
              ln -s ${modules.${m}.lidl}/${m}.lidl $out/${m}.lidl
              ln -s ${modules.${m}.lgx}/*.lgx $out/${m}.lgx
            '') providers}
          '';
        in
        # A root --override-input logos-lidl would break this silently: the bridge keeps its own reader.
        assert lib.assertMsg (logos-lidl.outPath == logos-json-rpc-bridge.inputs.logos-lidl.outPath)
          "logos-bridge: logos-lidl is not the bridge's reader; override logos-json-rpc-bridge/logos-lidl (LOGOS_DEV_LIDL) instead";
        cliEnv pkgs // {
          LOGOS_LOGOSCORE_BIN = lib.getExe' logos-logoscore-cli.packages.${system}.default "logoscore";
          LOGOS_BRIDGE_INSTALL_DIR = "${bridge.install}/modules";
          LOGOS_LIVE_MODULES_DIRS = lib.concatMapStringsSep ":" (m: "${modules.${m}.install}/modules") providers;
          # The install trees are unpacked from compressed .lgx files, so nix never sees what the
          # plugins' RUNPATH names (the bridge's libwebsockets): the plugin builds carry it in.
          LOGOS_BRIDGE_PLUGIN_BUILDS = toString (map (m: modules.${m}.lib) providers ++ [ bridge.lib ]);
          LOGOS_BRIDGE_PROVIDERS_DIR = "${providersDir}";
          LOGOS_BRIDGE_DOCS_CLI = lib.getExe' bridge.json-rpc-bridge-docs "json-rpc-bridge-docs";
          LOGOS_BRIDGE_METASCHEMAS = "${logos-json-rpc-bridge}/tests/metaschemas";
          LOGOS_LIDL_EXPECTED_REV = lidlRev;
          # This bridge holds one per-peer slot per connection (its keep-alive fix).
          LOGOS_BRIDGE_FIXES = "1";
        };

      stackShellHook = ''
        export PYTHONPATH="$PWD/src:${logos-logoscore-py}/src''${PYTHONPATH:+:$PYTHONPATH}"
        export QT_QPA_PLATFORM=offscreen
      '';

      # pytest against real bridges. Every daemon and module host is reaped at exit, even
      # if pytest dies; loopback only, so it runs in the Linux sandbox.
      mkIntegration = pkgs: name: python: tests: mkCheck pkgs name (stackEnv pkgs) ''
        export QT_QPA_PLATFORM=offscreen QT_FORCE_STDERR_LOGGING=1
        ${lib.optionalString pkgs.stdenv.isLinux ''
          export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
        ''}
        ${lib.optionalString pkgs.stdenv.isDarwin ''
          # macOS caps AF_UNIX paths at 104 bytes, and the build's TMPDIR is deep.
          TMPDIR=$(mktemp -d /tmp/lbpy.XXXXXX)
          export TMPDIR
        ''}
        export LOGOS_LIVE_RUN_DIR="$TMPDIR/run" LOGOS_BRIDGE_INTEGRATION=required
        export PYTHONPATH="$PWD/src:${logos-logoscore-py}/src"
        reap() {
          ${python.interpreter} -m logos_bridge.testing.live --reap "$LOGOS_LIVE_RUN_DIR" || true
          ${lib.optionalString pkgs.stdenv.isDarwin ''rm -rf "$TMPDIR"''}
        }
        trap reap EXIT
        ${python.interpreter} -m pytest ${tests} -v -p no:cacheprovider -m integration --durations=10
      '';

      # 3.10 is the oldest supported Python (websockets 15.0.1 here). That websockets
      # is not cached for Linux, and its own suite's deps fail in the sandbox.
      python310With = pkgs: extra: pkgs.python310.withPackages (ps:
        [ (ps.websockets.overridePythonAttrs (_: { doCheck = false; })) ] ++ extra ps);

      mkPackage = pkgs: pkgs.python3Packages.buildPythonPackage {
        pname = "logos-bridge";
        inherit version src;
        pyproject = true;
        build-system = [ pkgs.python3Packages.hatchling ];
        propagatedBuildInputs = [ pkgs.python3Packages.websockets ];
        # The suite runs in checks.unit / checks.unit-py310.
        doCheck = false;
        pythonImportsCheck = [
          "logos_bridge" "logos_bridge.testing" "logos_bridge.testing.docs"
          "logos_bridge.lidl" "logos_bridge.typed" "logos_bridge.codegen" "logos_bridge.dynamic"
          "logos_bridge.testing.live"
        ];
      };

      # A check over a writable copy of the source tree.
      mkCheck = pkgs: name: attrs: script: pkgs.runCommand name ({
        __darwinAllowLocalNetworking = true;
      } // attrs) ''
        cp -r ${src}/. .
        chmod -R u+w .
        export HOME=$PWD/home
        mkdir -p "$HOME"
        ${script}
        touch $out
      '';

      # pytest over the source tree; the fake bridge listens on loopback.
      mkUnit = pkgs: name: python: mkCheck pkgs name ({ nativeBuildInputs = [ python ]; } // cliEnv pkgs) ''
        export PYTHONPATH=$PWD/src
        ${python.interpreter} -m pytest tests/unit tests/typing -v -p no:cacheprovider
      '';

      # lidl-compat: the locked reader reads every consumed contract, and the
      # identity built-ins it injects are the ones this package mirrors.
      lidlCompat = builtins.toFile "lidl_compat.py" ''
        import subprocess, sys
        sys.path.insert(0, "src")
        from logos_bridge.digest import canonical_json
        from logos_bridge.lidl import IDENTITY_METHODS, Interface, TypeRef

        lidl, source = sys.argv[1:3]
        def cli(*args):
            return subprocess.run([lidl, *args, "--", source], check=True, capture_output=True).stdout
        cli("check", "--identity")
        plain = Interface.loads(cli("json"))
        served = Interface.loads(cli("json", "--identity"))
        served.check()
        assert canonical_json(plain.with_identity().to_json()) == canonical_json(served.to_json()), source
        derived = [m for m in served.methods if m.derived]
        assert [m.name for m in derived] == list(IDENTITY_METHODS), source
        assert all(not m.params and m.returns and m.returns.type == TypeRef.primitive("tstr") for m in derived)
        print(f"lidl-compat: {source}: {served.name} {served.version}, identity ok")
      '';
    in
    {
      packages = forAllSystems (pkgs: rec {
        logos-bridge = mkPackage pkgs;
        default = logos-bridge;
      });

      # One CI step per check: .github/workflows/ci.yml verifies the names match.
      checks = forAllSystems (pkgs:
        let
          python = pkgs.python3.withPackages (ps: [ ps.websockets ]);
          lidl = lidlExe pkgs;
        in
        {
          # CLI tests use the pinned lidl and lgx.
          unit = mkUnit pkgs "logos-bridge-unit"
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.websockets ps.jsonschema ]));

          unit-py310 = mkUnit pkgs "logos-bridge-unit-py310" (python310With pkgs (ps: [ ps.pytest ]));

          # The committed goldens (provenance aside), and byte-identical output on 3.10 and 3.13.
          codegen-golden = let py310 = python310With pkgs (_: [ ]); in
            mkCheck pkgs "logos-bridge-codegen-golden" { } ''
              ${python.interpreter} scripts/regen-goldens --check --lidl-cli ${lidl}
              ${py310.interpreter} scripts/regen-goldens --lidl-cli ${lidl} --out "$TMPDIR/py310"
              ${python.interpreter} scripts/regen-goldens --lidl-cli ${lidl} --out "$TMPDIR/py3"
              diff -r "$TMPDIR/py310" "$TMPDIR/py3"
              echo "codegen-golden: identical output on $(${py310.interpreter} --version) and $(${python.interpreter} --version)"
            '';

          typecheck =
            let mypy = pkgs.python3.withPackages (ps: [ ps.mypy ps.pytest ps.websockets ps.typing-extensions ]);
            in mkCheck pkgs "logos-bridge-typecheck" { } ''
              ${mypy.interpreter} -m mypy --strict --python-version 3.10 --cache-dir "$PWD/.mypy_cache" \
                src tests/goldens tests/typing
            '';

          # The vendored ASTs and edge outputs are what the pinned reader produces, the
          # Python identity/validator/serializer mirrors agree with it, and every vendored
          # copy equals its input's file. The meta-schemas are not vendored: integration-docs
          # reads the bridge's own, which this checks against the bridge's SOURCES.md.
          fixtures-drift = mkCheck pkgs "logos-bridge-fixtures-drift" { } ''
            ${python.interpreter} scripts/regen-fixtures --check --lidl-cli ${lidl} \
              --bridge ${logos-json-rpc-bridge} --test-modules ${logos-test-modules}
          '';

          # A real bridge under a logoscore daemon: calls, events, gating, limits,
          # the lifecycle canary, typed clients and contract identities.
          integration = mkIntegration pkgs "logos-bridge-integration"
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.websockets ]))
            "tests/integration --ignore=tests/integration/test_docs_conformance.py";

          # The served OpenRPC/OpenAPI/AsyncAPI documents equal json-rpc-bridge-docs' output,
          # validate against the bridge's meta-schemas, and describe captured traffic.
          integration-docs = mkIntegration pkgs "logos-bridge-integration-docs"
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.websockets ps.jsonschema ps.referencing ]))
            "tests/integration/test_docs_conformance.py";

          lidl-compat = mkCheck pkgs "logos-bridge-lidl-compat" { } ''
            version=$(${lidl} --version)
            echo "$version"
            case "$version" in
              *"(${lidlRev})") ;;
              *) echo "lidl-compat: expected the locked logos-lidl (${lidlRev}), got: $version" >&2; exit 1 ;;
            esac
            for contract in tests/fixtures/lidl/*.lidl; do
              ${python.interpreter} ${lidlCompat} ${lidl} "$contract"
            done
          '';
        });

      apps = forAllSystems (pkgs:
        let
          python = pkgs.python3.withPackages (ps: [ ps.websockets ]);
          # Run from the checkout: the scripts write under the current directory.
          mkApp = name: {
            type = "app";
            program = lib.getExe (pkgs.writeShellApplication {
              inherit name;
              runtimeInputs = [ python ];
              text = ''
                if [ ! -f scripts/${name} ] || [ ! -d src/logos_bridge ]; then
                  echo "${name}: run this from the root of the logos-json-rpc-bridge-py checkout" >&2
                  exit 2
                fi
                exec python scripts/${name} --lidl-cli ${lidlExe pkgs} "$@"
              '';
            });
          };
        in
        {
          regen-fixtures = mkApp "regen-fixtures";
          regen-goldens = mkApp "regen-goldens";

          # A daemon with the test providers and the bridge on 127.0.0.1:PORT (8645), until Ctrl-C.
          dev-bridge = {
            type = "app";
            program = lib.getExe (pkgs.writeShellApplication {
              name = "dev-bridge";
              text = lib.concatStrings
                (lib.mapAttrsToList (name: value: "export ${name}=${lib.escapeShellArg value}\n") (stackEnv pkgs))
              + ''
                export PYTHONPATH=${src}/src:${logos-logoscore-py}/src QT_QPA_PLATFORM=offscreen
                exec ${python.interpreter} ${src}/scripts/dev-bridge "$@"
              '';
            });
          };
        });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell ({
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.pytest ps.websockets ps.mypy ps.typing-extensions ps.jsonschema ps.hatchling
            ]))
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        } // cliEnv pkgs);

        # The default shell plus the integration stack: `pytest tests/integration` runs here.
        integration = pkgs.mkShell ({
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.websockets ps.jsonschema ps.referencing ]))
          ];
          shellHook = stackShellHook;
        } // stackEnv pkgs);
      });
    };
}
