from __future__ import annotations

import pytest

from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.domain.errors import FailureClass


def test_success_is_zero() -> None:
    assert SUCCESS == 0


@pytest.mark.parametrize("failure_class", sorted(FailureClass, key=lambda member: member.value))
def test_every_failure_class_maps_to_a_nonzero_exit_code(failure_class: FailureClass) -> None:
    assert exit_code_for(failure_class) != SUCCESS


def test_exit_codes_are_pairwise_distinct() -> None:
    codes = [exit_code_for(failure_class) for failure_class in FailureClass]

    assert len(codes) == len(set(codes))
