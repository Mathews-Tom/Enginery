"""The relay image recipe: its own digest, pins and verification, beside the worker's."""

from __future__ import annotations

import re

import pytest

from enginery.adapters.oci import image
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError, StaleEvidenceError

from .test_image_recipe import IMAGE_ID, OTHER_ID, FakeEngine


def _relay_labels(digest: str | None = None) -> dict[str, str]:
    return {
        image.RECIPE_DIGEST_LABEL: image.relay_recipe_digest() if digest is None else digest,
        image.RELAY_IMAGE_LABEL: "1",
    }


class RelayEngine(FakeEngine):
    """The recipe fake, except that a build stamps what ``Dockerfile.relay`` stamps."""

    def _build(self, args):  # type: ignore[no-untyped-def]
        completed = super()._build(args)
        if completed.returncode != 0:
            return completed
        labels = self.images[IMAGE_ID]
        for label in (
            image.RUNNER_IMAGE_LABEL,
            image.CLAUDE_CODE_LABEL,
            image.OMP_LABEL,
            image.BUN_LABEL,
        ):
            labels.pop(label, None)
        labels[image.RELAY_IMAGE_LABEL] = "1"
        return completed


def test_the_relay_recipe_digest_is_stable_and_not_the_worker_digest() -> None:
    digest = image.relay_recipe_digest()

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert digest == image.relay_recipe_digest()
    assert digest != image.recipe_digest()


@pytest.mark.parametrize("name", image.RELAY_RECIPE_FILES)
def test_the_relay_digest_changes_when_any_relay_file_changes(name: str) -> None:
    files = image.load_recipe(image.RELAY_RECIPE_FILES)
    original = image.compute_recipe_digest(files, image.RELAY_RECIPE_FILES)
    files[name] = files[name] + b"\n# changed\n"

    assert image.compute_recipe_digest(files, image.RELAY_RECIPE_FILES) != original


def test_the_relay_base_is_pinned_by_index_digest_and_the_digest_covers_it() -> None:
    files = image.load_recipe(image.RELAY_RECIPE_FILES)
    pins = dict(image.pinned_args(files[image.RELAY_DOCKERFILE_NAME]))

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", pins["PYTHON_BASE_DIGEST"])
    assert pins["PYTHON_BASE_IMAGE"].startswith("python:3.12")
    original = image.compute_recipe_digest(files, image.RELAY_RECIPE_FILES)
    files[image.RELAY_DOCKERFILE_NAME] = files[image.RELAY_DOCKERFILE_NAME].replace(
        pins["PYTHON_BASE_DIGEST"].encode(), ("sha256:" + "0" * 64).encode()
    )
    assert image.compute_recipe_digest(files, image.RELAY_RECIPE_FILES) != original


def test_the_relay_dockerfile_is_non_root_and_bakes_in_nothing_secret() -> None:
    dockerfile = image.load_recipe(image.RELAY_RECIPE_FILES)[image.RELAY_DOCKERFILE_NAME].decode()

    assert "USER ${RELAY_UID}:${RELAY_GID}" in dockerfile
    assert dockerfile.rstrip().splitlines()[-2].startswith("ENTRYPOINT")
    assert "dev.enginery.relay-image" in dockerfile
    for forbidden in ("TOKEN", "apt-get", "pip install", "curl", "ADD http"):
        assert forbidden not in dockerfile.replace("# ", "").split("FROM", 1)[1]


def test_the_relay_recipe_is_the_two_files_the_build_context_receives() -> None:
    engine = RelayEngine()

    identity = image.build_relay_image(engine)

    assert set(engine.context_files) == {image.RELAY_DOCKERFILE_NAME, image.RELAY_SCRIPT_NAME}
    assert identity.image_id == IMAGE_ID
    assert identity.recipe_digest == image.relay_recipe_digest()
    assert identity.harness_versions == ()


def test_build_relay_image_stamps_the_digest_and_tags_it_by_default() -> None:
    engine = RelayEngine()

    image.build_relay_image(engine)

    build = next(call for call in engine.calls if call[0] == "build")
    assert f"{image.RECIPE_DIGEST_LABEL}={image.relay_recipe_digest()}" in build
    assert str(next(path for path in build if path.endswith("Dockerfile.relay")))
    assert image.default_relay_tag() in build
    assert image.default_relay_tag().startswith("enginery-relay:")


def test_a_failed_relay_build_says_so() -> None:
    engine = RelayEngine(build_returncode=1, build_stderr="boom")

    with pytest.raises(MissingPrerequisiteError, match="relay image build failed"):
        image.build_relay_image(engine)


def test_a_relay_image_verifies_against_its_own_recipe() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, _relay_labels())

    identity = image.verify_relay_image(engine, IMAGE_ID)

    assert identity.image_id == IMAGE_ID
    assert identity.recipe_digest == image.relay_recipe_digest()


def test_a_worker_image_is_not_a_relay_image() -> None:
    engine = FakeEngine()
    engine.add_image(
        IMAGE_ID,
        {
            image.RECIPE_DIGEST_LABEL: image.recipe_digest(),
            image.RUNNER_IMAGE_LABEL: "1",
            image.CLAUDE_CODE_LABEL: "2.1.296",
            image.OMP_LABEL: "18.10.3",
        },
    )

    with pytest.raises(MissingPrerequisiteError, match="not an Enginery relay image"):
        image.verify_relay_image(engine, IMAGE_ID)


def test_a_relay_image_is_not_a_worker_image() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, _relay_labels())

    with pytest.raises(MissingPrerequisiteError, match="not an Enginery runner image"):
        image.verify_image(engine, IMAGE_ID)


def test_a_relay_image_from_another_recipe_is_stale() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, _relay_labels("sha256:" + "9" * 64))

    with pytest.raises(StaleEvidenceError, match="built from recipe"):
        image.verify_relay_image(engine, IMAGE_ID)


def test_a_missing_relay_image_or_label_is_refused() -> None:
    engine = FakeEngine()
    with pytest.raises(MissingPrerequisiteError, match="cannot be inspected"):
        image.verify_relay_image(engine, OTHER_ID)

    engine.add_image(IMAGE_ID, {image.RELAY_IMAGE_LABEL: "1"})
    with pytest.raises(MissingPrerequisiteError, match="lacks label"):
        image.verify_relay_image(engine, IMAGE_ID)


def test_a_relay_image_reference_must_be_an_exact_id() -> None:
    with pytest.raises(InvalidInputError, match="relay image id"):
        image.verify_relay_image(FakeEngine(), "enginery-relay:latest")
