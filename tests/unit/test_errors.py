from __future__ import annotations

import pytest

from enginery.domain import errors


def test_failure_class_has_the_fifteen_required_members() -> None:
    assert len(errors.FailureClass) == 15


@pytest.mark.parametrize(
    "failure_class", sorted(errors.FailureClass, key=lambda member: member.value)
)
def test_every_failure_class_has_exactly_one_dedicated_exception(
    failure_class: errors.FailureClass,
) -> None:
    matching = [
        exc_type
        for exc_type in vars(errors).values()
        if isinstance(exc_type, type)
        and issubclass(exc_type, errors.EngineryError)
        and exc_type is not errors.EngineryError
        and getattr(exc_type, "failure_class", None) is failure_class
    ]

    assert len(matching) == 1, f"expected exactly one exception type for {failure_class}"


def test_enginery_error_carries_message_and_details() -> None:
    error = errors.InvalidInputError("bad input", details={"field": "name"})

    assert str(error) == "bad input"
    assert error.details == {"field": "name"}
    assert error.failure_class is errors.FailureClass.INVALID_INPUT


def test_enginery_error_details_default_to_empty_mapping() -> None:
    error = errors.MissingPrerequisiteError("missing uv")

    assert error.details == {}


def test_enginery_error_is_a_real_exception() -> None:
    with pytest.raises(errors.PolicyDenialError, match="denied"):
        raise errors.PolicyDenialError("denied")
