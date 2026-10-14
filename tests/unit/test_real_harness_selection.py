"""Which binary the real-harness permission proofs run against.

The proofs are only meaningful for the harness version the execution catalog tests. These tests
need no harness binary and no container engine: they pin the selection logic that keeps a
host binary of another version (harnesses auto-upgrade) from being proven, or silently skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.execution_catalog import InstalledHost
from tests.support import real_harness
from tests.support.loopback_model import LoopbackModelServer
from tests.support.real_harness import (
    HostFacts,
    ImageFacts,
    ProofWorkspace,
    RealHarness,
    require_real_target,
    select_target,
)

CATALOG = "18.10.3"
MATCHING = HostFacts(found=True, installed_version=CATALOG, matches_catalog=True)
UPGRADED = HostFacts(found=True, installed_version="18.10.4", matches_catalog=False)
USABLE_IMAGE = ImageFacts(True, "omp 18.10.3 in image enginery-runner:abc")
NO_ENGINE = ImageFacts(False, "no qualified container engine is reachable: down")


def test_a_host_binary_of_the_catalog_version_is_used_even_when_the_image_is_usable() -> None:
    selection = select_target("omp", CATALOG, MATCHING, USABLE_IMAGE)

    assert selection.target == "host"


def test_an_upgraded_host_binary_is_replaced_by_the_image_binary_not_proven_or_skipped() -> None:
    selection = select_target("omp", CATALOG, UPGRADED, USABLE_IMAGE)

    assert selection.target == "image"
    assert "18.10.4" in selection.reason
    assert CATALOG in selection.reason


def test_an_upgraded_host_binary_without_a_usable_image_skips_naming_both() -> None:
    selection = select_target("omp", CATALOG, UPGRADED, NO_ENGINE)

    assert selection.target == "skip"
    assert "18.10.4" in selection.reason  # the host binary
    assert "no qualified container engine is reachable" in selection.reason  # the image


def test_a_missing_host_binary_without_a_usable_image_skips_naming_both() -> None:
    selection = select_target("omp", CATALOG, HostFacts(False, None, False), NO_ENGINE)

    assert selection.target == "skip"
    assert "no omp binary is installed" in selection.reason
    assert "worker image cannot be used" in selection.reason


def test_an_unidentifiable_host_binary_falls_back_to_the_image() -> None:
    selection = select_target("omp", CATALOG, HostFacts(True, None, False), USABLE_IMAGE)

    assert selection.target == "image"
    assert "did not identify itself" in selection.reason


def test_a_harness_not_wired_for_the_image_skips_instead_of_proving_another_version() -> None:
    selection = select_target("claude", "2.1.296", UPGRADED, None)

    assert selection.target == "skip"
    assert "18.10.4" in selection.reason
    assert "not wired" in selection.reason


def _stub_environment(
    monkeypatch: pytest.MonkeyPatch, *, installed: str | None, image: ImageFacts
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    probe = (
        (lambda executable: InstalledHost(installed, "x"))
        if installed is not None
        else (lambda executable: None)
    )
    monkeypatch.setitem(real_harness._PROBES, "omp", ("omp", probe))
    monkeypatch.setattr(
        real_harness,
        "_image_facts",
        lambda harness: (image, "enginery-runner:abc" if image.usable else None),
    )


def test_require_real_target_uses_the_image_when_the_host_differs_from_the_real_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_version = load_execution_catalog().host("omp").tested_executable_version
    _stub_environment(monkeypatch, installed="0.0.1-not-the-catalog", image=USABLE_IMAGE)

    target = require_real_target("omp")

    assert target.in_image
    assert target.image == "enginery-runner:abc"
    assert target.version == catalog_version
    assert target.binary is None


def test_a_host_binary_inside_the_accepted_range_but_not_tested_is_not_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = load_execution_catalog().host("omp")
    tested = host.tested_executable_version
    major, minor, patch = (int(part) for part in tested.split("."))
    untested = f"{major}.{minor}.{patch + 1}"
    assert host.version.accepts(untested)  # accepted by the catalog, never the tested binary
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setitem(
        real_harness._PROBES,
        "omp",
        ("omp", lambda found: InstalledHost(untested, host.protocol_fingerprint)),
    )
    monkeypatch.setattr(
        real_harness, "_image_facts", lambda harness: (USABLE_IMAGE, "enginery-runner:abc")
    )

    target = require_real_target("omp")

    assert target.in_image
    assert target.version == tested


def test_require_real_target_skips_with_a_reason_naming_host_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_environment(monkeypatch, installed="0.0.1-not-the-catalog", image=NO_ENGINE)

    with pytest.raises(pytest.skip.Exception) as skipped:
        require_real_target("omp")

    assert "0.0.1-not-the-catalog" in str(skipped.value)
    assert "no qualified container engine is reachable" in str(skipped.value)


def test_the_image_target_reaches_a_stub_bound_on_every_interface() -> None:
    real = RealHarness("omp", "omp", CATALOG, None, "enginery-runner:abc", "r")
    host = RealHarness("omp", "omp", CATALOG, Path("/opt/bin/omp"), None, "r")

    assert real.stub_host == "host.docker.internal"
    assert real.stub("pi-native", []).bind_host == "0.0.0.0"
    assert host.stub_host == "127.0.0.1"
    assert host.stub("pi-native", []).bind_host == "127.0.0.1"
    with LoopbackModelServer("pi-native", [], bind_host="127.0.0.1") as server:
        assert server.url_for("host.docker.internal").startswith("http://host.docker.internal:")
        assert server.base_url.startswith("http://127.0.0.1:")


def test_the_image_run_is_a_throwaway_hardened_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_run_cli(
        self: OciEngine,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        captured.append(tuple(args))
        return subprocess.CompletedProcess(tuple(args), 0, "", "")

    monkeypatch.setattr(OciEngine, "run_cli", fake_run_cli)
    proof = ProofWorkspace(
        root=tmp_path,
        workspace=tmp_path / "workspace",
        outside=tmp_path / "outside",
        remote=tmp_path / "remote.git",
        home=tmp_path / "home",
    )
    real = RealHarness("omp", "omp", CATALOG, None, "enginery-runner:abc", "r")

    real.run(
        ("omp", "--version"),
        proof,
        {"HOME": "/home/worker", "PATH": "/usr/bin"},
        home_files={".omp/agent/models.yml": "x"},
    )

    (arguments,) = captured
    assert arguments[:2] == ("run", "--rm")
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--user", "--pids-limit"):
        assert flag in arguments
    assert "--privileged" not in arguments
    assert arguments[arguments.index("--network") + 1] == "bridge"
    assert arguments[arguments.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    mounts = [arguments[i + 1] for i, value in enumerate(arguments) if value == "-v"]
    assert mounts == [
        f"{proof.workspace}:/workspace:rw",
        f"{proof.remote}:{proof.remote}:rw",
    ]
    assert arguments[-3:] == ("sh", "omp", "--version")
