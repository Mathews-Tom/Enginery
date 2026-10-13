"""Build and verify the isolated worker image.

The recipe is the packaged ``Dockerfile.worker`` plus the files it copies in (``worker_entry.py``,
the omp permission overlay, and the npm manifest and lockfile that pin every harness package and
all of its transitive dependencies by integrity hash). Its digest is stamped on the image as a
label at build time and re-derived from the installed package at verification time, so an image
built from a different recipe than the one this installation ships is refused.

The model-access relay image is built and verified the same way from its own recipe
(``Dockerfile.relay`` plus ``relay.py``), with its own digest and its own image label, so a
worker image is never accepted as a relay or the other way round.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Protocol

from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError, StaleEvidenceError
from enginery.domain.runner import ImageIdentity

DOCKERFILE_NAME = "Dockerfile.worker"
WORKER_ENTRY_NAME = "worker_entry.py"
OMP_POLICY_NAME = "omp-isolated-policy.yml"
PACKAGE_MANIFEST_NAME = "package.json"
PACKAGE_LOCK_NAME = "package-lock.json"
RECIPE_FILES: tuple[str, ...] = (
    DOCKERFILE_NAME,
    WORKER_ENTRY_NAME,
    OMP_POLICY_NAME,
    PACKAGE_MANIFEST_NAME,
    PACKAGE_LOCK_NAME,
)

RELAY_DOCKERFILE_NAME = "Dockerfile.relay"
RELAY_SCRIPT_NAME = "relay.py"
RELAY_RECIPE_FILES: tuple[str, ...] = (RELAY_DOCKERFILE_NAME, RELAY_SCRIPT_NAME)
RELAY_IMAGE_LABEL = "dev.enginery.relay-image"

RECIPE_DIGEST_LABEL = "dev.enginery.recipe-digest"
RUNNER_IMAGE_LABEL = "dev.enginery.runner-image"
CLAUDE_CODE_LABEL = "dev.enginery.claude-code"
OMP_LABEL = "dev.enginery.omp"
BUN_LABEL = "dev.enginery.bun"

BUILD_TIMEOUT_SECONDS = 3600.0
INSPECT_TIMEOUT_SECONDS = 60.0
_STDERR_TAIL_LINES = 40
_ARG_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*$", re.MULTILINE)
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ImageEngine(Protocol):
    """The container-engine surface image management needs (satisfied by ``OciEngine``)."""

    def run_cli(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def load_recipe(names: Sequence[str] = RECIPE_FILES) -> dict[str, bytes]:
    """Read recipe files from the installed package (works from a wheel)."""
    root = resources.files("enginery.adapters.oci").joinpath("assets")
    return {name: root.joinpath(name).read_bytes() for name in names}


def pinned_args(dockerfile: bytes) -> tuple[tuple[str, str], ...]:
    """The sorted ``ARG NAME=value`` pins declared by a Dockerfile."""
    text = dockerfile.decode("utf-8")
    return tuple(sorted((name, value) for name, value in _ARG_PATTERN.findall(text)))


def compute_recipe_digest(files: Mapping[str, bytes], names: Sequence[str] = RECIPE_FILES) -> str:
    """Digest recipe file bytes plus the sorted pinned-ARG set, deterministically.

    ``names`` lists the recipe files in a fixed order; the first is the Dockerfile whose ``ARG``
    pins are folded into the digest.
    """
    missing = [name for name in names if name not in files]
    if missing:
        raise InvalidInputError(f"recipe is missing files: {', '.join(missing)}")
    hasher = hashlib.sha256()
    for name in names:
        content = files[name]
        hasher.update(f"file {name} {len(content)}\n".encode())
        hasher.update(content)
        hasher.update(b"\n")
    for name, value in pinned_args(files[names[0]]):
        hasher.update(f"arg {name}={value}\n".encode())
    return f"sha256:{hasher.hexdigest()}"


def recipe_digest() -> str:
    """The digest of the recipe packaged with this installation."""
    return compute_recipe_digest(load_recipe())


def relay_recipe_digest() -> str:
    """The digest of the relay recipe packaged with this installation."""
    return compute_recipe_digest(load_recipe(RELAY_RECIPE_FILES), RELAY_RECIPE_FILES)


def default_tag(digest: str | None = None) -> str:
    """The local tag a build gets when none is requested (keeps the image from being dangling)."""
    value = digest if digest is not None else recipe_digest()
    return f"enginery-runner:{value.removeprefix('sha256:')[:12]}"


def default_relay_tag(digest: str | None = None) -> str:
    """The local tag a relay build gets when none is requested."""
    value = digest if digest is not None else relay_recipe_digest()
    return f"enginery-relay:{value.removeprefix('sha256:')[:12]}"


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def _inspect(
    engine: ImageEngine, reference: str, noun: str = "runner image"
) -> tuple[str, Mapping[str, str]]:
    completed = engine.run_cli(
        [
            "image",
            "inspect",
            "--format",
            '{{.Id}}{{"\\n"}}{{json .Config.Labels}}',
            reference,
        ],
        timeout=INSPECT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise MissingPrerequisiteError(
            f"{noun} {reference} cannot be inspected: {_tail(completed.stderr)}"
        )
    identifier, _, raw_labels = completed.stdout.strip().partition("\n")
    try:
        labels = json.loads(raw_labels) if raw_labels.strip() else {}
    except json.JSONDecodeError as error:
        raise MissingPrerequisiteError(
            f"{noun} {reference} reported unreadable labels: {error}"
        ) from error
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise MissingPrerequisiteError(f"{noun} {reference} reported malformed labels")
    return identifier.strip(), labels


def _identity(image_id: str, labels: Mapping[str, str], digest: str) -> ImageIdentity:
    versions = tuple(
        sorted(
            (name, labels[label])
            for name, label in (
                ("bun", BUN_LABEL),
                ("claude-code", CLAUDE_CODE_LABEL),
                ("omp", OMP_LABEL),
            )
            if label in labels
        )
    )
    return ImageIdentity(image_id=image_id, recipe_digest=digest, harness_versions=versions)


def _build_recipe_image(
    engine: ImageEngine,
    *,
    files: Mapping[str, bytes],
    names: Sequence[str],
    digest: str,
    image_tag: str,
    no_cache: bool,
    noun: str,
) -> str:
    """Build ``files`` with ``names[0]`` as the Dockerfile; return the built image's id."""
    with tempfile.TemporaryDirectory(prefix="enginery-image-") as directory:
        context = Path(directory)
        for name, content in files.items():
            (context / name).write_bytes(content)
        arguments = [
            "build",
            "--file",
            str(context / names[0]),
            "--label",
            f"{RECIPE_DIGEST_LABEL}={digest}",
            "--tag",
            image_tag,
        ]
        if no_cache:
            arguments.append("--no-cache")
        arguments.append(str(context))
        completed = engine.run_cli(arguments, timeout=BUILD_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise MissingPrerequisiteError(
            f"{noun} build failed (exit {completed.returncode}):\n{_tail(completed.stderr)}"
        )
    identified = engine.run_cli(
        ["image", "inspect", "--format", "{{.Id}}", image_tag], timeout=INSPECT_TIMEOUT_SECONDS
    )
    image_id = identified.stdout.strip()
    if identified.returncode != 0 or not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise MissingPrerequisiteError(
            f"built {noun} {image_tag} has no usable id: {_tail(identified.stderr)}"
        )
    return image_id


def build_image(
    engine: ImageEngine, *, tag: str | None = None, no_cache: bool = False
) -> ImageIdentity:
    """Build the worker image from the packaged recipe and return its exact identity."""
    files = load_recipe()
    digest = compute_recipe_digest(files)
    image_id = _build_recipe_image(
        engine,
        files=files,
        names=RECIPE_FILES,
        digest=digest,
        image_tag=tag if tag is not None else default_tag(digest),
        no_cache=no_cache,
        noun="runner image",
    )
    return verify_image(engine, image_id)


def verify_image(engine: ImageEngine, image_id: str) -> ImageIdentity:
    """Confirm ``image_id`` exists and was built from the recipe this installation ships."""
    if not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise InvalidInputError("runner image id must be sha256:<64 hex>")
    reported_id, labels = _inspect(engine, image_id)
    if reported_id != image_id:
        raise MissingPrerequisiteError(
            f"runner image {image_id} is not present (engine reported {reported_id or 'nothing'})"
        )
    if labels.get(RUNNER_IMAGE_LABEL) != "1":
        raise MissingPrerequisiteError(f"image {image_id} is not an Enginery runner image")
    for label in (CLAUDE_CODE_LABEL, OMP_LABEL):
        if not labels.get(label):
            raise MissingPrerequisiteError(f"runner image {image_id} lacks label {label}")
    recorded = labels.get(RECIPE_DIGEST_LABEL)
    if not recorded:
        raise MissingPrerequisiteError(f"runner image {image_id} lacks label {RECIPE_DIGEST_LABEL}")
    expected = recipe_digest()
    if recorded != expected:
        raise StaleEvidenceError(
            f"runner image {image_id} was built from recipe {recorded}; "
            f"this installation ships {expected}",
            details={"image_id": image_id, "recorded": recorded, "expected": expected},
        )
    return _identity(image_id, labels, expected)


def build_relay_image(
    engine: ImageEngine, *, tag: str | None = None, no_cache: bool = False
) -> ImageIdentity:
    """Build the model-access relay image from the packaged recipe; return its exact identity."""
    files = load_recipe(RELAY_RECIPE_FILES)
    digest = compute_recipe_digest(files, RELAY_RECIPE_FILES)
    image_id = _build_recipe_image(
        engine,
        files=files,
        names=RELAY_RECIPE_FILES,
        digest=digest,
        image_tag=tag if tag is not None else default_relay_tag(digest),
        no_cache=no_cache,
        noun="relay image",
    )
    return verify_relay_image(engine, image_id)


def verify_relay_image(engine: ImageEngine, image_id: str) -> ImageIdentity:
    """Confirm ``image_id`` exists and was built from the relay recipe this installation ships."""
    if not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise InvalidInputError("relay image id must be sha256:<64 hex>")
    reported_id, labels = _inspect(engine, image_id, "relay image")
    if reported_id != image_id:
        raise MissingPrerequisiteError(
            f"relay image {image_id} is not present (engine reported {reported_id or 'nothing'})"
        )
    if labels.get(RELAY_IMAGE_LABEL) != "1":
        raise MissingPrerequisiteError(f"image {image_id} is not an Enginery relay image")
    recorded = labels.get(RECIPE_DIGEST_LABEL)
    if not recorded:
        raise MissingPrerequisiteError(f"relay image {image_id} lacks label {RECIPE_DIGEST_LABEL}")
    expected = relay_recipe_digest()
    if recorded != expected:
        raise StaleEvidenceError(
            f"relay image {image_id} was built from recipe {recorded}; "
            f"this installation ships {expected}",
            details={"image_id": image_id, "recorded": recorded, "expected": expected},
        )
    return ImageIdentity(image_id=image_id, recipe_digest=expected)
