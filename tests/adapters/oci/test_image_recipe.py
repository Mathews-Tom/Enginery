"""The worker image recipe: digest identity, build invocation, and admission-time verification."""

from __future__ import annotations

import ast
import itertools
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.adapters.oci import image
from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError, StaleEvidenceError

IMAGE_ID = "sha256:" + "ab" * 32
OTHER_ID = "sha256:" + "cd" * 32


def _completed(
    args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


class FakeEngine:
    """An engine that stores built images' labels the way a real daemon would."""

    def __init__(self, *, build_returncode: int = 0, build_stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.context_files: dict[str, bytes] = {}
        self.images: dict[str, dict[str, str]] = {}
        self.tags: dict[str, str] = {}
        self.build_returncode = build_returncode
        self.build_stderr = build_stderr

    def add_image(self, image_id: str, labels: dict[str, str] | None) -> None:
        self.images[image_id] = {} if labels is None else dict(labels)

    def run_cli(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[0] == "build":
            return self._build(args)
        if args[:2] == ["image", "inspect"]:
            return self._inspect(args)
        raise AssertionError(f"unexpected engine call {list(args)}")

    def _build(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        context = Path(args[-1])
        self.context_files = {path.name: path.read_bytes() for path in context.iterdir()}
        if self.build_returncode != 0:
            return _completed(args, self.build_returncode, stderr=self.build_stderr)
        labels = dict(
            pair.split("=", 1) for flag, pair in itertools.pairwise(args) if flag == "--label"
        )
        labels.update(
            {
                image.RUNNER_IMAGE_LABEL: "1",
                image.CLAUDE_CODE_LABEL: "2.1.296",
                image.OMP_LABEL: "18.10.3",
                image.BUN_LABEL: "1.3.14",
            }
        )
        self.images[IMAGE_ID] = labels
        self.tags[args[args.index("--tag") + 1]] = IMAGE_ID
        return _completed(args)

    def _inspect(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        reference = args[-1]
        image_id = self.tags.get(reference, reference)
        if image_id not in self.images:
            return _completed(args, 1, stderr=f"Error: No such image: {reference}")
        if args[args.index("--format") + 1] == "{{.Id}}":
            return _completed(args, stdout=f"{image_id}\n")
        return _completed(args, stdout=f"{image_id}\n{json.dumps(self.images[image_id])}\n")


def _runner_labels(digest: str | None = None) -> dict[str, str]:
    return {
        image.RECIPE_DIGEST_LABEL: image.recipe_digest() if digest is None else digest,
        image.RUNNER_IMAGE_LABEL: "1",
        image.CLAUDE_CODE_LABEL: "2.1.296",
        image.OMP_LABEL: "18.10.3",
        image.BUN_LABEL: "1.3.14",
    }


def test_recipe_digest_is_a_stable_prefixed_sha256() -> None:
    first = image.recipe_digest()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    assert image.recipe_digest() == first
    assert first == image.compute_recipe_digest(image.load_recipe())


@pytest.mark.parametrize("name", image.RECIPE_FILES)
def test_recipe_digest_changes_when_any_recipe_file_changes(name: str) -> None:
    files = image.load_recipe()
    original = image.compute_recipe_digest(files)
    files[name] = files[name] + b"\n# changed\n"
    assert image.compute_recipe_digest(files) != original


def test_recipe_digest_covers_the_pinned_arguments() -> None:
    files = image.load_recipe()
    original = image.compute_recipe_digest(files)
    files[image.DOCKERFILE_NAME] = files[image.DOCKERFILE_NAME].replace(
        b"ARG BUN_VERSION=1.3.14", b"ARG BUN_VERSION=1.3.15"
    )
    assert files[image.DOCKERFILE_NAME] != image.load_recipe()[image.DOCKERFILE_NAME]
    assert image.compute_recipe_digest(files) != original


def test_recipe_digest_does_not_confuse_file_boundaries() -> None:
    files = image.load_recipe()
    moved = dict(files)
    moved[image.WORKER_ENTRY_NAME] = files[image.WORKER_ENTRY_NAME][:-5]
    moved[image.OMP_POLICY_NAME] = (
        files[image.WORKER_ENTRY_NAME][-5:] + files[image.OMP_POLICY_NAME]
    )
    assert image.compute_recipe_digest(moved) != image.compute_recipe_digest(files)


def test_recipe_digest_rejects_an_incomplete_recipe() -> None:
    files = image.load_recipe()
    del files[image.WORKER_ENTRY_NAME]
    with pytest.raises(InvalidInputError, match=image.WORKER_ENTRY_NAME):
        image.compute_recipe_digest(files)


def test_packaged_recipe_pins_every_input() -> None:
    dockerfile = image.load_recipe()[image.DOCKERFILE_NAME]
    pins = dict(image.pinned_args(dockerfile))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", pins["NODE_BASE_DIGEST"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", pins["UV_DIGEST"])
    assert pins["CLAUDE_CODE_VERSION"] == "2.1.296"
    assert pins["OMP_VERSION"] == "18.10.3"
    assert pins["BUN_VERSION"] == "1.3.14"
    assert pins["WORKER_UID"] == pins["WORKER_GID"] == "65532"
    assert list(image.pinned_args(dockerfile)) == sorted(image.pinned_args(dockerfile))
    text = dockerfile.decode()
    assert re.search(r"^ENTRYPOINT \[\]$", text, re.MULTILINE)
    assert re.search(r"^CMD \[\]$", text, re.MULTILINE)
    assert re.search(r"^FROM \$\{NODE_BASE_IMAGE\}@\$\{NODE_BASE_DIGEST\}$", text, re.MULTILINE)
    assert re.search(r"^USER \$\{WORKER_UID\}:\$\{WORKER_GID\}$", text, re.MULTILINE)


def test_dockerfile_harness_pins_equal_the_execution_catalog() -> None:
    """The image carries exactly the harness versions the catalog tested, inside its range."""
    pins = dict(image.pinned_args(image.load_recipe()[image.DOCKERFILE_NAME]))
    catalog = load_execution_catalog()

    for harness, pin in (("omp", "OMP_VERSION"), ("claude-code", "CLAUDE_CODE_VERSION")):
        host = catalog.host(harness)
        assert pins[pin] == host.tested_executable_version
        assert host.version.accepts(pins[pin])


def _manifest_and_lock() -> tuple[dict[str, object], dict[str, object]]:
    files = image.load_recipe()
    return (
        json.loads(files[image.PACKAGE_MANIFEST_NAME]),
        json.loads(files[image.PACKAGE_LOCK_NAME]),
    )


def test_npm_manifest_pins_equal_the_dockerfile_pins() -> None:
    manifest, _ = _manifest_and_lock()
    pins = dict(image.pinned_args(image.load_recipe()[image.DOCKERFILE_NAME]))

    assert manifest["dependencies"] == {
        "@anthropic-ai/claude-code": pins["CLAUDE_CODE_VERSION"],
        "@oh-my-pi/pi-coding-agent": pins["OMP_VERSION"],
        "bun": pins["BUN_VERSION"],
    }


def test_the_lockfile_locks_every_transitive_package_by_integrity() -> None:
    manifest, lock = _manifest_and_lock()
    packages = lock["packages"]
    assert isinstance(packages, dict)
    pins = dict(image.pinned_args(image.load_recipe()[image.DOCKERFILE_NAME]))

    assert lock["lockfileVersion"] == 3
    assert packages[""]["dependencies"] == manifest["dependencies"]
    assert len(packages) > 100  # the transitive closure, not just the three top-level packages
    unlocked = [
        name
        for name, entry in packages.items()
        if name
        and not (
            str(entry.get("integrity", "")).startswith("sha512-")
            and str(entry.get("resolved", "")).startswith("https://registry.npmjs.org/")
        )
    ]
    assert unlocked == []
    assert (
        packages["node_modules/@anthropic-ai/claude-code"]["version"] == pins["CLAUDE_CODE_VERSION"]
    )
    assert packages["node_modules/@oh-my-pi/pi-coding-agent"]["version"] == pins["OMP_VERSION"]
    assert packages["node_modules/bun"]["version"] == pins["BUN_VERSION"]


def test_the_dockerfile_installs_harnesses_only_from_the_lockfile() -> None:
    text = image.load_recipe()[image.DOCKERFILE_NAME].decode()

    assert re.search(r"^COPY package\.json package-lock\.json /opt/enginery/harness/$", text, re.M)
    assert "npm ci " in text
    assert "npm install" not in text


@pytest.mark.parametrize("name", [image.PACKAGE_MANIFEST_NAME, image.PACKAGE_LOCK_NAME])
def test_the_lock_assets_are_part_of_the_recipe(name: str) -> None:
    assert name in image.RECIPE_FILES
    assert name in image.load_recipe()


def test_worker_entry_uses_only_the_standard_library() -> None:
    source = image.load_recipe()[image.WORKER_ENTRY_NAME].decode()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= set(sys.stdlib_module_names)


def test_build_stamps_the_recipe_digest_and_sends_exactly_the_recipe_as_context() -> None:
    engine = FakeEngine()

    identity = image.build_image(engine)

    digest = image.recipe_digest()
    build = engine.calls[0]
    assert build[0] == "build"
    assert f"{image.RECIPE_DIGEST_LABEL}={digest}" in build
    assert build[build.index("--tag") + 1] == image.default_tag(digest)
    assert "--no-cache" not in build
    assert engine.context_files == image.load_recipe()
    assert identity.image_id == IMAGE_ID
    assert identity.recipe_digest == digest
    assert identity.harness_versions == (
        ("bun", "1.3.14"),
        ("claude-code", "2.1.296"),
        ("omp", "18.10.3"),
    )
    assert not Path(build[-1]).exists()


def test_build_honours_tag_and_no_cache() -> None:
    engine = FakeEngine()

    image.build_image(engine, tag="enginery-runner:test", no_cache=True)

    build = engine.calls[0]
    assert build[build.index("--tag") + 1] == "enginery-runner:test"
    assert "--no-cache" in build


def test_build_failure_reports_the_stderr_tail() -> None:
    noise = "\n".join(f"step {number}" for number in range(200))
    engine = FakeEngine(build_returncode=1, build_stderr=noise + "\nE: Unable to locate package")

    with pytest.raises(MissingPrerequisiteError) as raised:
        image.build_image(engine)

    message = str(raised.value)
    assert "Unable to locate package" in message
    assert "step 199" in message
    assert "step 0\n" not in message


def test_verify_returns_the_identity_of_a_current_image() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, _runner_labels())

    identity = image.verify_image(engine, IMAGE_ID)

    assert identity.image_id == IMAGE_ID
    assert identity.recipe_digest == image.recipe_digest()
    assert dict(identity.harness_versions) == {
        "bun": "1.3.14",
        "claude-code": "2.1.296",
        "omp": "18.10.3",
    }


def test_verify_refuses_an_absent_image() -> None:
    with pytest.raises(MissingPrerequisiteError, match="No such image"):
        image.verify_image(FakeEngine(), IMAGE_ID)


def test_verify_refuses_an_image_built_from_another_recipe() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, _runner_labels("sha256:" + "00" * 32))

    with pytest.raises(StaleEvidenceError) as raised:
        image.verify_image(engine, IMAGE_ID)

    assert raised.value.details["recorded"] == "sha256:" + "00" * 32
    assert raised.value.details["expected"] == image.recipe_digest()


@pytest.mark.parametrize(
    "drop",
    [
        image.RECIPE_DIGEST_LABEL,
        image.RUNNER_IMAGE_LABEL,
        image.CLAUDE_CODE_LABEL,
        image.OMP_LABEL,
    ],
)
def test_verify_refuses_an_image_missing_a_required_label(drop: str) -> None:
    labels = _runner_labels()
    del labels[drop]
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, labels)

    with pytest.raises(MissingPrerequisiteError):
        image.verify_image(engine, IMAGE_ID)


def test_verify_refuses_an_image_with_no_labels_at_all() -> None:
    engine = FakeEngine()
    engine.add_image(IMAGE_ID, None)

    with pytest.raises(MissingPrerequisiteError, match="not an Enginery runner image"):
        image.verify_image(engine, IMAGE_ID)


def test_verify_refuses_an_engine_that_reports_a_different_id() -> None:
    engine = FakeEngine()
    engine.add_image(OTHER_ID, _runner_labels())
    engine.tags[IMAGE_ID] = OTHER_ID

    with pytest.raises(MissingPrerequisiteError, match="not present"):
        image.verify_image(engine, IMAGE_ID)


@pytest.mark.parametrize("bad", ["enginery-runner:dev", "sha256:abc", "ab" * 32, ""])
def test_verify_requires_an_exact_image_id(bad: str) -> None:
    engine = FakeEngine()
    with pytest.raises(InvalidInputError):
        image.verify_image(engine, bad)
    assert engine.calls == []
