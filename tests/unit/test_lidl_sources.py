"""Contract sources: the lidl and lgx CLIs, AST files, LGX packages, a running bridge."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import warnings
from pathlib import Path

import pytest
from fixture_util import (
    EDGE_DIR,
    LIDL_DIR,
    ast_doc,
    contract_assets,
    edge_doc,
    fake_lgx_cli,
    fake_lidl_cli,
    interface,
    lidl_text,
    real_lgx_cli,
    real_lidl_cli,
    write_script,
)

from logos_bridge import AsyncBridgeClient, BridgeClient, BridgeWarning, ClientTimeout, DiscoveryPending
from logos_bridge.digest import contract_sha256
from logos_bridge.lidl import Interface
from logos_bridge.lidl_sources import (
    CliNotFound,
    ContractRejected,
    ContractSourceError,
    LgxCli,
    LidlCli,
    SourceUnavailable,
    find_lgx_cli,
    find_lidl_cli,
    interface_from_module_info,
    load_ast,
    load_from_bridge,
    load_from_bridge_sync,
    load_lgx,
    load_lidl,
    variant_names,
)
from logos_bridge.models import ModuleInfo
from logos_bridge.testing import FakeBridge, ThreadedFakeBridge, async_test


@pytest.fixture
def no_cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Neither env var set, and a PATH with no lidl/lgx on it."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.delenv("LOGOS_LIDL_CLI", raising=False)
    monkeypatch.delenv("LOGOS_LGX_CLI", raising=False)
    monkeypatch.setenv("PATH", str(empty))
    return empty


@pytest.fixture
def fake_lidl(tmp_path: Path, no_cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cli = fake_lidl_cli(tmp_path / "bin")
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(cli))
    return cli


# ---------------------------------------------------------------- discovery


def test_the_cli_is_found_by_argument_then_env_then_path(
    tmp_path: Path, no_cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CliNotFound, match="set LOGOS_LIDL_CLI or put lidl on PATH"):
        find_lidl_cli()
    with pytest.raises(CliNotFound, match="logos-package#lgx"):
        find_lgx_cli()
    on_path = fake_lidl_cli(no_cli_env)
    assert find_lidl_cli() == str(on_path)
    other = fake_lidl_cli(tmp_path / "other")
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(other))
    assert find_lidl_cli() == str(other)
    explicit = fake_lidl_cli(tmp_path / "explicit")
    assert find_lidl_cli(explicit) == str(explicit)
    monkeypatch.setenv("LOGOS_LIDL_CLI", str(tmp_path / "missing"))
    with pytest.raises(CliNotFound, match="given by LOGOS_LIDL_CLI"):
        find_lidl_cli()
    plain = tmp_path / "not-executable"
    plain.write_text("")
    with pytest.raises(CliNotFound, match="given by argument"):
        find_lgx_cli(plain)
    assert CliNotFound.exit_code == SourceUnavailable.exit_code == 3 and ContractRejected.exit_code == 4


def test_a_cli_that_cannot_run(tmp_path: Path, no_cli_env: Path) -> None:
    broken = tmp_path / "lidl"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    with pytest.raises(CliNotFound, match="cannot run"):
        LidlCli(broken).version()


# ---------------------------------------------------------------- lidl CLI


def test_the_lidl_cli_wrapper(fake_lidl: Path, tmp_path: Path) -> None:
    cli = LidlCli()
    assert cli.version() == "lidl 0.0.0 (fake)" and repr(cli).startswith("<LidlCli ")
    assert cli.json(LIDL_DIR / "mini_module.lidl") == ast_doc("mini_module")
    assert cli.json(EDGE_DIR / "noreturn.lidl", identity=False) == edge_doc("noreturn")
    assert cli.fmt(LIDL_DIR / "mini_module.lidl") == lidl_text("mini_module")
    with pytest.raises(SourceUnavailable, match="cannot read"):
        cli.json(tmp_path / "missing.lidl")
    broken = tmp_path / "broken.lidl"
    broken.write_text("parse error")
    with pytest.raises(ContractRejected, match="broken.lidl:1:1: Expected 'module'"):
        cli.json(broken)
    with pytest.raises(ContractRejected, match="generator-owned built-in"):
        cli.json(EDGE_DIR / "identity_lidl.lidl")


def test_odd_lidl_cli_output(tmp_path: Path, no_cli_env: Path) -> None:
    text = write_script(tmp_path / "a", "lidl", "import sys\nprint('not json')\n")
    with pytest.raises(ContractRejected, match="not JSON"):
        LidlCli(text).json(LIDL_DIR / "mini_module.lidl")
    array = write_script(tmp_path / "b", "lidl", "import sys\nprint('[]')\n")
    with pytest.raises(ContractRejected, match="not an AST object"):
        LidlCli(array).json(LIDL_DIR / "mini_module.lidl")
    with pytest.raises(ContractRejected, match=r"check --json printed \[\]"):
        LidlCli(array).check(LIDL_DIR / "mini_module.lidl")
    usage = write_script(tmp_path / "c", "lidl", "import sys\nprint('lidl: unknown option', file=sys.stderr)\n"
                                               "sys.exit(1)\n")
    with pytest.raises(SourceUnavailable, match="json failed: lidl: unknown option"):
        LidlCli(usage).json(LIDL_DIR / "mini_module.lidl")
    with pytest.raises(SourceUnavailable, match="--version failed"):
        LidlCli(usage).version()


def test_the_real_lidl_cli() -> None:
    cli = LidlCli(real_lidl_cli())
    assert cli.version().startswith("lidl ")
    for name in ("mini_module", "storage_module"):
        assert cli.json(LIDL_DIR / f"{name}.lidl") == ast_doc(name)
        assert cli.fmt(LIDL_DIR / f"{name}.lidl") == lidl_text(name)
        assert cli.check(LIDL_DIR / f"{name}.lidl") == (0, {"errors": [], "warnings": []})
    status, report = cli.check(EDGE_DIR / "invalid.lidl")
    assert status == 5 and report == Interface.from_json(edge_doc("invalid")).lidl_report()


# ---------------------------------------------------------------- sources


def test_load_lidl(fake_lidl: Path) -> None:
    source = load_lidl(LIDL_DIR / "mini_module.lidl")
    assert source.kind == "lidl" and source.name == "mini_module"
    assert source.interface == interface("mini_module")
    assert source.text == lidl_text("mini_module")
    assert source.contract_sha256 == hashlib.sha256((LIDL_DIR / "mini_module.lidl").read_bytes()).hexdigest()
    assert source.interface_sha256 == interface("mini_module").interface_sha256()
    assert source.shape_sha256 == interface("mini_module").shape_sha256()
    assert source.reader == "lidl 0.0.0 (fake)" and source.notes == ()
    assert source.describe() == f"lidl {LIDL_DIR / 'mini_module.lidl'}"
    plain = load_lidl(EDGE_DIR / "noreturn.lidl", identity=False)
    assert not plain.interface.has_identity
    with pytest.raises(ContractRejected, match="declares module 'mini_module', not 'other'"):
        load_lidl(LIDL_DIR / "mini_module.lidl", module="other")
    with pytest.raises(ContractRejected, match="Duplicate method definition 'f'"):
        load_lidl(EDGE_DIR / "invalid.lidl")
    legacy = load_lidl(EDGE_DIR / "legacy_void.lidl")
    assert legacy.notes and "not in canonical form" in legacy.notes[0]
    with pytest.raises(SourceUnavailable, match="cannot read"):
        load_lidl(EDGE_DIR / "missing.lidl")


def test_load_lidl_needs_utf8(fake_lidl: Path, tmp_path: Path) -> None:
    bad = tmp_path / "mini_module.lidl"
    bad.write_bytes(b"module \xff {}")
    with pytest.raises(ContractRejected, match="not UTF-8"):
        load_lidl(bad)


def test_load_lidl_with_the_real_cli() -> None:
    source = load_lidl(LIDL_DIR / "storage_module.lidl", lidl_cli=real_lidl_cli())
    assert source.interface_sha256 == interface("storage_module").interface_sha256()
    assert source.contract_sha256 == "9f6bd141a1401b14ec151b579fd1e1076ba7916929f54101fd6843501dee92ac"
    assert source.notes == ()


def test_load_ast(tmp_path: Path) -> None:
    source = load_ast(Path(__file__).parents[1] / "fixtures" / "ast" / "test_fullapi_cpp.json")
    assert source.kind == "ast" and source.text is None and source.contract_sha256 is None
    assert source.interface == interface("test_fullapi_cpp")
    injected = load_ast(EDGE_DIR / "noreturn.json")
    assert injected.interface.has_identity
    assert not load_ast(EDGE_DIR / "noreturn.json", identity=False).interface.has_identity
    forward = load_ast(EDGE_DIR / "forward.json")
    assert forward.interface.interface_sha256() == Interface.from_json(edge_doc("forward")).with_identity() \
        .interface_sha256()
    with pytest.raises(ContractRejected, match="unknown type kind"):
        load_ast(EDGE_DIR / "forward.json", allow_unknown_types=False)
    with pytest.raises(ContractRejected, match="generator-owned"):
        load_ast(EDGE_DIR / "identity_lidl.json")
    (tmp_path / "text.json").write_text("{")
    with pytest.raises(ContractRejected, match="is not JSON"):
        load_ast(tmp_path / "text.json")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(ContractRejected, match="holds a list"):
        load_ast(tmp_path / "list.json")
    (tmp_path / "shape.json").write_text('{"name": 1}')
    with pytest.raises(ContractRejected, match="/name: expected string"):
        load_ast(tmp_path / "shape.json")
    with pytest.raises(SourceUnavailable):
        load_ast(tmp_path / "missing.json")


# ------------------------------------------------------------------- LGX


def lgx_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *contracts: str) -> tuple[Path, Path]:
    assets = contract_assets(tmp_path / "assets", *contracts)
    lgx = fake_lgx_cli(tmp_path / "lgxbin", mode, assets)
    monkeypatch.setenv("LOGOS_LGX_CLI", str(lgx))
    package = tmp_path / "fake.lgx"
    package.write_bytes(b"not opened by this package")
    return package, lgx.with_suffix(".py").with_suffix(".log")


@pytest.mark.parametrize("mode", ["assets-only", "no-assets-only"])
def test_load_lgx_selects_the_module_among_dependency_contracts(
    fake_lidl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    package, log = lgx_env(tmp_path, monkeypatch, mode, "mini_module", "storage_module")
    mini = load_lgx(package, "mini_module")
    storage = load_lgx(package, "storage_module")
    assert mini.kind == "lgx" and mini.interface == interface("mini_module")
    assert storage.interface == interface("storage_module")
    assert storage.contract_sha256 == "9f6bd141a1401b14ec151b579fd1e1076ba7916929f54101fd6843501dee92ac"
    assert "module storage_module, package fake_pkg 1.0.0" in storage.location
    calls = log.read_text().splitlines()
    assert calls[0] == f"verify {package}" and calls[1] == f"manifest {package} --json"
    extract = [c for c in calls if c.startswith("extract") and "--help" not in c]
    if mode == "assets-only":
        assert all("--assets-only" in c and "--variant" not in c for c in extract)
    else:
        assert all("--variant darwin-arm64" in c for c in extract)  # the first variant, sorted


def test_load_lgx_error_paths(fake_lidl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package, _ = lgx_env(tmp_path / "a", monkeypatch, "assets-only", "storage_module")
    with pytest.raises(SourceUnavailable, match=r"no contract for 'mini_module' .* it carries: storage_module"):
        load_lgx(package, "mini_module")
    package, _ = lgx_env(tmp_path / "b", monkeypatch, "no-contracts")
    with pytest.raises(SourceUnavailable, match="it carries: none"):
        load_lgx(package, "mini_module")
    package, _ = lgx_env(tmp_path / "c", monkeypatch, "verify-fails", "mini_module")
    with pytest.raises(ContractRejected, match="(?s)failed lgx verify: .*Not valid gzip data") as rejected:
        load_lgx(package, "mini_module")
    assert rejected.value.exit_code == 4
    with pytest.raises(SourceUnavailable, match="no such package"):
        load_lgx(tmp_path / "missing.lgx", "mini_module")
    monkeypatch.setenv("LOGOS_LGX_CLI", str(tmp_path / "no-lgx-here"))
    with pytest.raises(CliNotFound) as missing:
        load_lgx(package, "mini_module")
    assert missing.value.exit_code == 3


def test_lgx_wrapper_details(tmp_path: Path, no_cli_env: Path) -> None:
    failing = write_script(tmp_path / "x", "lgx", "import sys\nprint('Error: boom', file=sys.stderr)\n"
                                                  "sys.exit(1)\n")
    cli = LgxCli(failing)
    assert repr(cli).startswith("<LgxCli ")
    with pytest.raises(ContractRejected, match="lgx manifest .* failed: Error: boom"):
        cli.manifest(tmp_path / "p.lgx")
    assert not cli.supports_assets_only()
    with pytest.raises(ContractRejected, match="has no variants"):
        cli.extract_assets(tmp_path / "p.lgx", tmp_path / "out", {"main": {}})
    with pytest.raises(ContractRejected, match="--variant v .* failed: Error: boom"):
        cli.extract_assets(tmp_path / "p.lgx", tmp_path / "out", {"main": {"v": "x"}})
    odd = write_script(tmp_path / "y", "lgx", "print('[1]')\n")
    with pytest.raises(ContractRejected, match="printed list"):
        LgxCli(odd).manifest(tmp_path / "p.lgx")
    garbage = write_script(tmp_path / "z", "lgx", "print('{')\n")
    with pytest.raises(ContractRejected, match="invalid JSON"):
        LgxCli(garbage).manifest(tmp_path / "p.lgx")
    assert variant_names({"main": {"b": 1, "a": 2}}) == ["b", "a"] and variant_names({}) == []


def build_real_package(tmp_path: Path, lgx: str) -> Path:
    """An LGX carrying its own contract and a dependency's, built only with the lgx CLI."""
    work = tmp_path / "pkg"
    (work / "build").mkdir(parents=True)
    (work / "build" / "mini_module_plugin.so").write_bytes(b"not a real plugin")
    contract_assets(work / "assets", "mini_module", "storage_module")
    steps = [["create", "mini_module"]]
    steps += [["add", "mini_module.lgx", "--variant", variant, "--files", "build/mini_module_plugin.so",
               "--assets", "assets", "--yes"] for variant in ("linux-amd64", "darwin-arm64")]
    steps.append(["verify", "mini_module.lgx"])
    for step in steps:
        subprocess.run([lgx, *step], cwd=work, check=True, capture_output=True)
    return work / "mini_module.lgx"


def logged_cli(directory: Path, real: str) -> tuple[Path, Path]:
    """``real`` behind a wrapper that appends each command line to a log."""
    wrapper = write_script(directory, "lgx", f"""
        import os, pathlib, sys
        with pathlib.Path(__file__).with_suffix(".log").open("a") as fh:
            fh.write(" ".join(sys.argv[1:]) + "\\n")
        os.execv({real!r}, [{real!r}, *sys.argv[1:]])
    """)
    return wrapper, wrapper.with_suffix(".log")


def test_load_lgx_with_the_real_clis(tmp_path: Path) -> None:
    lgx, lidl = real_lgx_cli(), real_lidl_cli()
    package = build_real_package(tmp_path, lgx)
    logged, log = logged_cli(tmp_path / "logged", lgx)
    for name in ("mini_module", "storage_module"):
        source = load_lgx(package, name, lgx_cli=logged, lidl_cli=lidl)
        assert source.interface_sha256 == interface(name).interface_sha256()
        assert source.contract_sha256 == hashlib.sha256((LIDL_DIR / f"{name}.lidl").read_bytes()).hexdigest()
    # The pinned lgx (logos-package#42) extracts assets alone; the fake-lgx tests cover the fallback.
    extract = [c for c in log.read_text().splitlines() if c.startswith("extract") and "--help" not in c]
    assert len(extract) == 2 and all("--assets-only" in c and "--variant" not in c for c in extract), extract
    with pytest.raises(SourceUnavailable, match="it carries: mini_module, storage_module"):
        load_lgx(package, "test_fullapi_cpp", lgx_cli=lgx, lidl_cli=lidl)
    corrupt = tmp_path / "corrupt.lgx"
    corrupt.write_bytes(b"garbage")
    with pytest.raises(ContractRejected, match="failed lgx verify"):
        load_lgx(corrupt, "mini_module", lgx_cli=lgx, lidl_cli=lidl)


# --------------------------------------------------------------- bridge


def typed_module(fake: FakeBridge, name: str = "mini_module", *, text: str | None = None,
                 status: str = "ok") -> None:
    doc = ast_doc(name)
    module = fake.module(name, [m["name"] for m in doc["methods"]], [e["name"] for e in doc["events"]],
                         interface=doc, status=status)
    module.contract_sha256 = contract_sha256(lidl_text(name))
    fake.on_call(name, "lidl", lidl_text(name) if text is None else text)


@async_test
async def test_load_from_bridge_reads_lidl_and_checks_its_digest(fake_lidl: Path) -> None:
    async with FakeBridge() as fake:
        typed_module(fake)
        source = await load_from_bridge(fake.url, "mini_module")
        assert source.kind == "bridge" and source.text == lidl_text("mini_module")
        assert source.interface == interface("mini_module") and source.notes == ()
        assert source.info is not None and source.info.typed
        assert source.reader == "lidl 0.0.0 (fake)"
        assert (await fake.wait_for_request("rpc.call")).params == {
            "module": "mini_module", "method": "lidl", "params": []}


@async_test
async def test_load_from_bridge_refuses_untyped_and_mismatched_modules(fake_lidl: Path) -> None:
    async with FakeBridge() as fake:
        fake.module("legacy", ["ping"], status="untyped")
        broken = fake.module("broken", ["reset", "lidl"], status="invalid")
        broken.interface_error = "lidl() did not parse: 3:17: expected ')'"
        fake.module("old_bridge", ["ping"])
        typed_module(fake, text=lidl_text("mini_module") + " ")
        typed_module(fake, "storage_module", text="")
        fake.on_call("storage_module", "lidl", 42)
        with pytest.raises(SourceUnavailable, match=r"legacy does not expose LIDL \(status: untyped\)"):
            await load_from_bridge(fake.url, "legacy")
        with pytest.raises(SourceUnavailable, match=r"\(status: invalid\): lidl\(\) did not parse"):
            await load_from_bridge(fake.url, "broken")
        with pytest.raises(SourceUnavailable, match="status: no lidl discovery"):
            await load_from_bridge(fake.url, "old_bridge")
        with pytest.raises(ContractRejected, match="hashes to .* but the bridge reports contract_sha256"):
            await load_from_bridge(fake.url, "mini_module")
        with pytest.raises(ContractRejected, match="answered int, not a string"):
            await load_from_bridge(fake.url, "storage_module")


@async_test
async def test_load_from_bridge_waits_out_pending(fake_lidl: Path) -> None:
    async with FakeBridge() as fake:
        typed_module(fake, status="pending")

        async def later() -> None:
            await fake.wait_for_request("rpc.schema", count=2)
            typed_module(fake)

        task = asyncio.ensure_future(later())
        source = await load_from_bridge(fake.url, "mini_module", discovery_wait=5)
        await task
        assert source.interface.name == "mini_module"
        typed_module(fake, "storage_module", status="pending")
        with pytest.raises(DiscoveryPending) as excinfo:
            await load_from_bridge(fake.url, "storage_module", discovery_wait=0.2)
        assert isinstance(excinfo.value, ClientTimeout) and "storage_module" in str(excinfo.value)


@async_test
async def test_load_from_bridge_without_a_cli_uses_the_served_interface(no_cli_env: Path) -> None:
    async with FakeBridge() as fake:
        typed_module(fake)
        source = await load_from_bridge(fake.url, "mini_module")
        assert source.reader == "bridge" and "no lidl CLI" in source.notes[0]
        assert source.interface == interface("mini_module")
        with pytest.raises(CliNotFound):
            await load_from_bridge(fake.url, "mini_module", lidl_cli="/nonexistent/lidl")


@async_test
async def test_a_reader_mismatch_is_a_note(fake_lidl: Path) -> None:
    async with FakeBridge() as fake:
        doc = ast_doc("mini_module")
        doc["description"] = "the bridge's reader saw something else"
        module = fake.module("mini_module", ["put"], interface=doc, status="ok")
        module.contract_sha256 = contract_sha256(lidl_text("mini_module"))
        fake.on_call("mini_module", "lidl", lidl_text("mini_module"))
        source = await load_from_bridge(fake.url, "mini_module")
        assert source.notes and "interface_sha256 differs" in source.notes[0]


def test_load_from_bridge_sync(fake_lidl: Path) -> None:
    with ThreadedFakeBridge() as fake:
        doc = ast_doc("test_fullapi_cpp")
        module = fake.module("test_fullapi_cpp", ["whoAmI"], interface=doc, status="ok")
        assert module.status == "ok"
        fake.on_call("test_fullapi_cpp", "lidl", lidl_text("test_fullapi_cpp"))
        source = load_from_bridge_sync(fake.url, "test_fullapi_cpp")
        assert source.interface == interface("test_fullapi_cpp")


def test_interface_from_module_info() -> None:
    doc = ast_doc("mini_module")
    info = ModuleInfo.from_json({"module": "mini_module", "interface_status": "ok", "interface": doc,
                                 "interface_sha256": interface("mini_module").interface_sha256()})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert interface_from_module_info(info) == interface("mini_module")
    wrong = ModuleInfo.from_json({**info.raw, "interface_sha256": "0" * 64})
    with pytest.warns(BridgeWarning, match="but the bridge reports interface_sha256 0000"):
        interface_from_module_info(wrong)
    untyped = ModuleInfo.from_json({"module": "m", "interface_status": "invalid", "interface_error": "boom"})
    with pytest.raises(SourceUnavailable, match=r"\(status: invalid\): boom"):
        interface_from_module_info(untyped)
    assert issubclass(SourceUnavailable, ContractSourceError)


# -------------------------------------------------------- wait_for_module


@async_test
async def test_wait_for_module(no_cli_env: Path) -> None:
    async with FakeBridge() as fake:
        fake.module("legacy", ["ping"])
        fake.module("late", ["ping"], status="pending")
        async with AsyncBridgeClient(fake.url) as client:
            assert (await client.wait_for_module("legacy")).interface_status is None
            with pytest.raises(DiscoveryPending, match="contract discovery of late"):
                await client.wait_for_module("late", timeout=0.1)
            with pytest.raises(ValueError):
                await client.wait_for_module("late", timeout=-1)

            async def resolve() -> None:
                await asyncio.sleep(0.2)
                fake.module("late", ["ping"], status="untyped")

            task = asyncio.ensure_future(resolve())
            info = await client.wait_for_module("late", timeout=None)
            await task
            assert info.interface_status == "untyped"


def test_wait_for_module_blocking() -> None:
    with ThreadedFakeBridge() as fake:
        fake.module("m", ["ping"], status="untyped")
        with BridgeClient(fake.url) as bridge:
            assert bridge.wait_for_module("m", timeout=1).interface_status == "untyped"

