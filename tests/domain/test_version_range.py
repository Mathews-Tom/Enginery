"""Tests for enginery.domain.version_range."""

from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.version_range import Version, VersionRange


class TestVersion:
    def test_parses_a_plain_numeric_version(self) -> None:
        version = Version.parse("18.10.3")

        assert (version.major, version.minor, version.patch) == (18, 10, 3)
        assert str(version) == "18.10.3"

    def test_orders_numerically_per_component_not_lexically(self) -> None:
        assert Version.parse("18.10.3") > Version.parse("18.9.30")
        assert Version.parse("2.1.300") > Version.parse("2.1.296")
        assert Version.parse("10.0.0") > Version.parse("9.99.99")
        assert Version.parse("18.10.3") == Version.parse("18.10.3")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "18",
            "18.10",
            "18.10.3.1",
            "v18.10.3",
            "18.11.0-beta.1",
            "18.11.0+build.5",
            "18.11.0rc1",
            "18.010.3",
            "01.0.0",
            "18.10.-3",
            " 18.10.3",
            "18.10.3 ",
            "18.10.3\n",
            "18.10.x",
            "\uff11\uff18.10.3",
        ],
    )
    def test_rejects_anything_that_is_not_plain_numeric(self, text: str) -> None:
        with pytest.raises(InvalidInputError, match="plain numeric"):
            Version.parse(text)

    def test_rejects_negative_components(self) -> None:
        with pytest.raises(InvalidInputError, match="non-negative"):
            Version(major=1, minor=-1, patch=0)


class TestVersionRange:
    OMP = VersionRange.parse(">=18.10.3,<19.0.0")

    def test_parses_and_renders_the_canonical_form(self) -> None:
        assert str(self.OMP) == ">=18.10.3,<19.0.0"
        assert self.OMP.lower == Version.parse("18.10.3")
        assert self.OMP.upper == Version.parse("19.0.0")

    def test_lower_bound_is_inclusive_and_upper_bound_is_exclusive(self) -> None:
        assert self.OMP.accepts("18.10.3")
        assert self.OMP.accepts("18.11.0")
        assert self.OMP.accepts("18.99.99")
        assert not self.OMP.accepts("18.10.2")
        assert not self.OMP.accepts("19.0.0")
        assert not self.OMP.accepts("19.0.1")

    def test_claude_range_boundaries(self) -> None:
        claude = VersionRange.parse(">=2.1.296,<3.0.0")

        assert claude.accepts("2.1.296")
        assert claude.accepts("2.1.300")
        assert claude.accepts("2.10.0")
        assert not claude.accepts("2.1.295")
        assert not claude.accepts("3.0.0")

    @pytest.mark.parametrize(
        "banner",
        ["18.11.0-beta.1", "18.11.0+build", "18.11", "v18.11.0", "", "garbage", "18.11.0 "],
    )
    def test_a_banner_that_is_not_plain_numeric_is_never_accepted(self, banner: str) -> None:
        assert not self.OMP.accepts(banner)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "18.10.3",
            "==18.10.3",
            ">18.10.3,<19.0.0",
            "<=19.0.0,>=18.10.3",
            "<19.0.0,>=18.10.3",
            ">=18.10.3",
            "<19.0.0",
            ">=18.10.3,<19.0.0,!=18.10.5",
            ">=18.10.3, <19.0.0",
            " >=18.10.3,<19.0.0",
            ">=18.10.3,<=19.0.0",
            ">=18.10,<19.0.0",
            ">=18.10.3,<19.0.0-rc.1",
            "~=18.10.3",
            "^18.10.3",
            ">=18.10.3,<19.*",
        ],
    )
    def test_rejects_any_other_operator_or_shape(self, text: str) -> None:
        with pytest.raises(InvalidInputError):
            VersionRange.parse(text)

    @pytest.mark.parametrize("text", [">=19.0.0,<18.10.3", ">=18.10.3,<18.10.3"])
    def test_rejects_an_empty_or_inverted_range(self, text: str) -> None:
        with pytest.raises(InvalidInputError, match="lower bound below"):
            VersionRange.parse(text)
