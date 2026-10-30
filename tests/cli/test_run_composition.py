"""The shared composition preserves bound starts and the public watch document."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.cli.run_composition import (
    read_only_service,
    runtime_for_run,
    start_bound_run,
    watch_document,
)
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import RunId
from enginery.engine.runtime import RUN_AGGREGATE_TYPE
from enginery.ledger.service import LedgerService
from enginery.workflows.run_intent import stage1_request_from_state
from tests.cli.test_engine_runs import _lifecycle, _request, _start
from tests.support.execution import bound_document


def test_bound_start_refuses_a_request_that_disagrees_with_its_compiled_configuration(
    tmp_path: Path,
) -> None:
    document = bound_document(_request(tmp_path))
    request = stage1_request_from_state(document)
    tampered = replace(request, repair_limit=request.repair_limit + 1)
    raw_request = {**tampered.initial_state(), "effective_config": document["effective_config"]}
    ledger = LedgerService.open(tmp_path / "ledger.sqlite")
    try:
        with pytest.raises(InvalidInputError, match=r"repair_limit.*configuration snapshot"):
            start_bound_run(ledger, owner="factory", request=tampered, raw_request=raw_request)
        assert (
            ledger.read_projection(
                aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=str(request.run.id)
            )
            is None
        )
    finally:
        ledger.close()


def test_malformed_start_request_leaves_no_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")
    assert _start(database, path) == 2
    assert "Stage 1 run projection run must be a mapping" in capsys.readouterr().err
    assert not database.exists()


def test_shared_watch_document_preserves_the_stage1_watch_shape_and_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    request_path = tmp_path / "request.json"
    document = bound_document(_request(tmp_path))
    request_path.write_text(json.dumps(document), encoding="utf-8")
    assert _start(database, request_path) == 0
    capsys.readouterr()
    assert _lifecycle(database, "next", "--run-id", "run-stage1") == 0
    cli_document = json.loads(capsys.readouterr().out)

    ledger = LedgerService.open(database)
    try:
        request = stage1_request_from_state(document)
        runtime = runtime_for_run(ledger, owner="factory", run_id=str(request.run.id))
        progression = read_only_service(
            runtime=runtime, ledger=ledger, request=request
        ).next_action(RunId("run-stage1"))
        shared_document = watch_document(ledger, runtime, progression)
        assert shared_document == cli_document
        assert shared_document["action_taken"] is None
        assert shared_document["status"] == "created"
        assert set(shared_document) == {
            "run_id",
            "status",
            "aggregate_version",
            "action_taken",
            "next_action",
            "nodes",
            "hold",
            "execution",
            "cost_budget",
        }
        assert shared_document["next_action"] == "qualify"
        assert shared_document["nodes"] == []
        assert shared_document["cost_budget"] == {"value": None, "enforced": False}
        advanced_document = watch_document(
            ledger, runtime, progression, action_taken=progression.action.value
        )
        assert advanced_document == {**shared_document, "action_taken": "qualify"}
    finally:
        ledger.close()
