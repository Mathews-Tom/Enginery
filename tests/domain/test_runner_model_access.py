"""``model_access`` on a runner selection: persisted only when the relay is granted."""

from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    LEGACY_RUNNER_SELECTION,
    MODEL_ACCESS_NONE,
    MODEL_ACCESS_RELAY,
    RelayEndpoint,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
)

IMAGE = "sha256:" + "ab" * 32


def _selection(model_access: str = MODEL_ACCESS_NONE) -> RunnerSelection:
    return RunnerSelection.isolated(image=IMAGE, profile=RunnerProfile(), model_access=model_access)


def test_a_selection_without_model_access_keeps_its_persisted_shape() -> None:
    state = _selection().to_state()

    assert "model_access" not in state
    assert RunnerSelection.from_state(state) == _selection()
    assert _selection().model_access == MODEL_ACCESS_NONE


def test_the_relay_grant_is_persisted_round_trips_and_changes_the_state() -> None:
    granted = _selection(MODEL_ACCESS_RELAY)
    state = granted.to_state()

    assert state["model_access"] == "relay"
    assert RunnerSelection.from_state(state) == granted
    assert state != _selection().to_state()


def test_a_persisted_selection_from_before_model_access_decodes_as_none() -> None:
    state = _selection().to_state()
    assert "model_access" not in state

    assert RunnerSelection.from_state(state).model_access == MODEL_ACCESS_NONE
    assert "model_access" not in LEGACY_RUNNER_SELECTION.to_state()


def test_an_unknown_model_access_is_refused() -> None:
    with pytest.raises(InvalidInputError, match="model access is unknown"):
        _selection("everything")
    state = {**_selection().to_state(), "model_access": "direct"}
    with pytest.raises(InvalidInputError, match="model access is unknown"):
        RunnerSelection.from_state(state)


def test_a_trusted_local_selection_cannot_carry_a_relay_grant() -> None:
    with pytest.raises(InvalidInputError, match="only to isolated runs"):
        RunnerSelection(
            trust_mode=RunnerSelection.trusted_local().trust_mode,
            profile_name=RunnerSelection.trusted_local().profile_name,
            model_access=MODEL_ACCESS_RELAY,
        )


def test_a_context_carries_a_relay_endpoint_exactly_when_the_relay_is_granted() -> None:
    profile = RunnerProfile()
    granted = _selection(MODEL_ACCESS_RELAY)

    context = RunnerContext(
        selection=granted, profile=profile, receipt_root="/receipts", relay=RelayEndpoint()
    )
    assert context.relay == RelayEndpoint()
    with pytest.raises(InvalidInputError, match="exactly when"):
        RunnerContext(selection=granted, profile=profile, receipt_root="/receipts")
    with pytest.raises(InvalidInputError, match="exactly when"):
        RunnerContext(
            selection=_selection(),
            profile=profile,
            receipt_root="/receipts",
            relay=RelayEndpoint(),
        )


def test_the_default_relay_endpoint_is_the_fixed_alias_and_port() -> None:
    endpoint = RelayEndpoint()

    assert endpoint.url == "http://enginery-relay:8080"
    assert endpoint.dummy_credential == "enginery-relay"
