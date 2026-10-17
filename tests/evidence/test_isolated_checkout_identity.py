"""Returned isolated workspaces never become host Git repositories."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from enginery.adapters.checkout_identity import verified_returned_build
from enginery.adapters.oci.store import replace_tree, write_tree_archive
from enginery.application.behavioral_checks import BehavioralAssertion, BehavioralCheck
from enginery.application.runner_ports import AttestedProcess
from enginery.domain.digests import Digest
from enginery.domain.errors import StaleEvidenceError
from enginery.domain.execution_receipt import RunnerReceiptRef
from enginery.ledger.artifact_store import ArtifactStore
from enginery.workflows.behavioral_acceptance import BehavioralAcceptanceService
from tests.evidence.test_behavioral_acceptance import _current, _repository


def _plant(workspace: Path, marker: Path) -> None:
    metadata = workspace / ".git"
    (metadata / "info").mkdir(parents=True, exist_ok=True)
    with (metadata / "config").open("a") as stream:
        stream.write(f'[filter "pwn"]\n\tclean = "sh -c \'touch {marker}; cat\'"\n')
    (metadata / "info" / "attributes").write_text("* filter=pwn\n")


def test_plain_git_status_executes_planted_filter_control(tmp_path: Path) -> None:
    checkout, _ = _repository(tmp_path, "print('changed', end='')\n")
    marker = tmp_path / "HOST-EXECUTED"
    _plant(checkout, marker)
    # Same-size dirty bytes force Git to consult the clean filter, not its size shortcut.
    (checkout / "behavior.py").write_text("print('altered', end='')\n")
    subprocess.run(["git", "status", "--porcelain"], cwd=checkout, check=True, capture_output=True)
    assert marker.exists(), "control must prove the filter executes under plain host git"


def test_isolated_returned_git_config_never_executes_on_host(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('changed', end='')\n")
    marker = tmp_path / "HOST-EXECUTED"
    artifacts = ArtifactStore(tmp_path / "artifacts")
    reference = RunnerReceiptRef(
        "isolated-oci",
        artifacts.publish_bytes(b'{"runner":"receipt"}', media_type="application/json"),
        "sha256:" + "a" * 64,
        Digest.of_bytes(b"profile"),
    )
    calls = 0

    def container(argv: tuple[str, ...], workspace: Path) -> AttestedProcess:
        nonlocal calls
        calls += 1
        assert not (workspace / ".git").exists(), "host Git metadata must never enter runner"
        staging = tmp_path / "container-workspace"
        shutil.copytree(workspace, staging)
        _plant(staging, marker)
        buffer = io.BytesIO()
        write_tree_archive(str(staging), buffer, uid=0, gid=0, max_bytes=1 << 30, max_entries=1000)
        buffer.seek(0)
        replace_tree(buffer, str(workspace), max_bytes=1 << 30, max_entries=1000)
        payload = json.dumps({"returncode": 0, "stdout": "changed", "stderr": ""})
        return AttestedProcess(argv, 0, payload, "", runner_receipt=reference)

    custody = tmp_path / "evaluator"
    custody.mkdir(mode=0o700)
    service = BehavioralAcceptanceService(
        artifacts, custody, isolated=True, command_runner=container, calibration_python="python3"
    )
    check = BehavioralCheck(
        "behavior",
        "command",
        (BehavioralAssertion("stdout", "equals", "changed"),),
        ("constant_result", "wrong_build"),
        argv=("python3", "-I", "behavior.py"),
    )
    inputs = _current((check,), checkout, head)
    with pytest.raises(StaleEvidenceError, match="returned Git metadata"):
        service.run(
            checks=(check,),
            cwd=checkout,
            source_revision=inputs.source_revision,
            base_revision=inputs.base_revision,
            head_revision=head,
            configuration_snapshot_digest=inputs.configuration_snapshot_digest,
            evaluator=inputs.evaluator,
            environment=inputs.environment,
        )
    assert calls == 1
    assert not marker.exists()


@pytest.mark.parametrize("link", ["symlink", "hardlink", "directory"])
def test_returned_tracked_files_must_be_private_regular_bytes(tmp_path: Path, link: str) -> None:
    checkout, head = _repository(tmp_path, "print('changed')\n")
    returned = tmp_path / "returned"
    returned.mkdir()
    original = checkout / "behavior.py"
    target = returned / "behavior.py"
    if link == "symlink":
        target.symlink_to(original)
    elif link == "hardlink":
        target.hardlink_to(original)
    else:
        target.mkdir()
    with pytest.raises(StaleEvidenceError, match="behavioral_build_mismatch"):
        verified_returned_build(returned, checkout, head)
