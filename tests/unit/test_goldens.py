"""The committed goldens (tests/goldens): they regenerate identically, and they work."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from codegen_util import load_module
from fixture_util import CONTRACTS, LIDL_DIR, ROOT, fake_lidl_cli, interface, lidl_text

from logos_bridge import AsyncBridgeClient
from logos_bridge.codegen import run
from logos_bridge.digest import contract_sha256
from logos_bridge.testing import FakeBridge, FakeProvider, async_test

GOLDENS = ROOT / "tests" / "goldens"
SCRIPT = ROOT / "scripts" / "regen-goldens"
HINT = "nix run .#regen-goldens"


@pytest.fixture(scope="module")
def fake_cli(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fake_lidl_cli(tmp_path_factory.mktemp("bin"))


def golden(name: str) -> ModuleType:
    return load_module(GOLDENS / f"{name}_client.py")


def test_every_contract_has_both_goldens() -> None:
    expected = sorted([f"{n}_client.py" for n in CONTRACTS] + [f"{n}.md" for n in CONTRACTS])
    assert sorted(p.name for p in GOLDENS.iterdir() if p.is_file()) == expected


@pytest.mark.parametrize("name", CONTRACTS)
@pytest.mark.parametrize(("command", "suffix", "extra"), [
    ("python", "_client.py", ["--regen-hint", HINT]),
    ("markdown", ".md", []),
])
def test_goldens_regenerate_identically(fake_cli: Path, name: str, command: str, suffix: str,
                                        extra: list[str]) -> None:
    err = io.StringIO()
    argv = [command, "--lidl", str(LIDL_DIR / f"{name}.lidl"), "--lidl-cli", str(fake_cli),
            "-o", str(GOLDENS / f"{name}{suffix}"), "--check", *extra]
    assert run(argv, stdout=io.StringIO(), stderr=err) == 0, err.getvalue()


@pytest.mark.parametrize("name", CONTRACTS)
def test_golden_text_is_normalized(name: str) -> None:
    for path in (GOLDENS / f"{name}_client.py", GOLDENS / f"{name}.md"):
        data = path.read_bytes()
        assert b"\r" not in data and data.endswith(b"\n") and not data.endswith(b"\n\n")
        assert not re.search(rb"[ \t]\n", data), path.name
        data.decode("utf-8")


@pytest.mark.parametrize("name", CONTRACTS)
def test_golden_digests_match_the_fixtures(name: str) -> None:
    module = golden(name)
    iface = interface(name)
    assert module.INTERFACE_SHA256 == iface.interface_sha256()
    assert module.CONTRACT_SHA256 == contract_sha256(lidl_text(name))
    assert module.SHAPE_SHA256 == iface.shape_sha256()
    head = (GOLDENS / f"{name}_client.py").read_text(encoding="utf-8").split("\n", 12)
    assert f"# INTERFACE_SHA256: {module.INTERFACE_SHA256}" in head
    assert f"# CONTRACT_SHA256: {module.CONTRACT_SHA256}" in head
    assert f"# SHAPE_SHA256: {module.SHAPE_SHA256}" in head
    assert f"# Regenerate: {HINT}" in head
    assert f"# Source: lidl tests/fixtures/lidl/{name}.lidl" in head


def test_the_script_checks_and_reports_drift(fake_cli: Path, tmp_path: Path) -> None:
    command = [sys.executable, str(SCRIPT), "--lidl-cli", str(fake_cli), "--root", str(ROOT)]
    done = subprocess.run([*command, "--check"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count(": ok\n") == 2 * len(CONTRACTS)
    copy = tmp_path / "goldens"
    shutil.copytree(GOLDENS, copy)
    edited = copy / "mini_module.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    done = subprocess.run([*command, "--check", "--out", str(copy)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 1
    assert "regen-goldens: markdown mini_module: exit 1" in done.stdout
    done = subprocess.run([*command, "--out", str(copy)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and not edited.read_text(encoding="utf-8").endswith("edited\n")
    done = subprocess.run([*command, "--check", "--out", str(copy)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout


class Notes:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.notes: dict[str, dict[str, object]] = {}

    def put(self, note: dict[str, object]) -> dict[str, object]:
        self.notes[str(note["id"])] = note
        self.provider.emit("added", note)
        return note

    def find(self, id: str, prefix: str | None) -> dict[str, object]:
        note = self.notes.get(id)
        return {"success": note is not None, "value": note, "error": None if note else "no such note"}

    def clear(self) -> None:
        self.notes.clear()


@async_test
async def test_the_mini_golden_against_a_fake_provider() -> None:
    mini = golden("mini_module")
    async with FakeBridge() as fake:
        provider = FakeProvider(interface("mini_module"), None, lidl_text=lidl_text("mini_module"))
        provider.impl = Notes(provider)
        provider.install(fake)
        async with AsyncBridgeClient(fake.url) as bridge:
            client = mini.AsyncMiniModuleClient(bridge)
            report = await client.check_compat()
            assert report.level == "exact"
            async with client.on_added() as sub:
                note = mini.Note(id="n1", body=b"\x00\xff", tag="x")
                assert await client.put(note) == note
                event = await sub.get(timeout=5)
                assert isinstance(event, mini.AddedEvent) and event.note == note
            found = await client.find("n1")
            assert found.success and found.value["body"] == {"_bytes": "AP8"}
            assert not (await client.find("n2")).success
            assert await client.clear() is None
            assert await client.lidl() == lidl_text("mini_module")
