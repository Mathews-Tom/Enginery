"""Deterministic, network-free probes for the Stage 2 release brokers and the
Stage 3 ``LocalServiceDeploymentAdapter``, for ``enginery adapter doctor``.

Every probe here matches the local-CLI-tool-presence idiom every other
adapter's ``probe()`` already follows (``gh --version``, ``uv --version``,
``claude --version``, and so on): a local executable/file check, never a
live GitHub/PyPI/local-service network call. GitHub repository and PyPI
project identity default to the real values an installed distribution's
own package metadata reports (``importlib.metadata``, which — unlike
``pyproject.toml`` — is present after a genuine clean install, not only
inside a source checkout), never a fabricated placeholder.
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

from enginery.adapters.github import GitHubAdapterConfig, GitHubReleaseAdapter
from enginery.adapters.local_service import LocalServiceDeploymentAdapter
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.adapter_types import AdapterAvailability, AdapterStatus, ProviderKind

_DISTRIBUTION = "enginery"
_DEFAULT_DEPLOYMENT_APP_SCRIPT = Path("fixtures/enginery-stage3-local-service/app.py")
_DEFAULT_DEPLOYMENT_ARTIFACTS_ROOT = Path(".enginery/stage3-deployment/artifacts")
_DEFAULT_DEPLOYMENT_STATE_ROOT = Path(".enginery/stage3-deployment/state")
_DOCTOR_CREDENTIAL_REFERENCE = "doctor-probe"
_PYPI_INDEX_URL = "https://pypi.org/simple/"
_PYPI_PUBLISH_URL = "https://upload.pypi.org/legacy/"
_PYPI_JSON_API_BASE = "https://pypi.org/pypi"


def _unmeasured(kind: ProviderKind, *, detail: str) -> AdapterStatus:
    return AdapterStatus(
        kind=kind, availability=AdapterAvailability.MISCONFIGURED, fingerprint=None, detail=detail
    )


def _repository_from_metadata(*, distribution: str = _DISTRIBUTION) -> str | None:
    try:
        project_urls = metadata.metadata(distribution).get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for entry in project_urls:
        if not isinstance(entry, str):
            continue
        label, _, url = entry.partition(",")
        if label.strip() != "Repository":
            continue
        url = url.strip()
        if url.startswith("https://github.com/"):
            resolved: str = url.removeprefix("https://github.com/").rstrip("/")
            return resolved
    return None


def _project_name_from_metadata(*, distribution: str = _DISTRIBUTION) -> str | None:
    try:
        name = metadata.metadata(distribution).get("Name")
    except metadata.PackageNotFoundError:
        return None
    return name if isinstance(name, str) and name.strip() else None


def _github_release_broker_status(*, repository: str | None, executable: str) -> AdapterStatus:
    resolved_repository = repository or _repository_from_metadata()
    if resolved_repository is None:
        return _unmeasured(
            ProviderKind.RELEASE,
            detail=(
                "no GitHub repository configured and none found in installed package "
                "metadata (Project-URL: Repository)"
            ),
        )
    config = GitHubAdapterConfig(
        repository=resolved_repository,
        credential_reference=_DOCTOR_CREDENTIAL_REFERENCE,
        executable=executable,
    )
    return GitHubReleaseAdapter(config).probe()


def _pypi_release_broker_status(*, project_name: str | None, executable: str) -> AdapterStatus:
    resolved_project_name = project_name or _project_name_from_metadata()
    if resolved_project_name is None:
        return _unmeasured(
            ProviderKind.RELEASE,
            detail="no PyPI project name configured and none found in installed package metadata",
        )
    config = PyPiAdapterConfig(
        project_name=resolved_project_name,
        index_url=_PYPI_INDEX_URL,
        publish_url=_PYPI_PUBLISH_URL,
        json_api_base=_PYPI_JSON_API_BASE,
        executable=executable,
    )
    return PyPiAdapter(config).probe()


def _local_service_deployment_status(
    *,
    app_script: Path | None,
    artifacts_root: Path | None,
    state_root: Path | None,
    python_executable: str | None,
) -> AdapterStatus:
    adapter = LocalServiceDeploymentAdapter(
        artifacts_root=artifacts_root or _DEFAULT_DEPLOYMENT_ARTIFACTS_ROOT,
        state_root=state_root or _DEFAULT_DEPLOYMENT_STATE_ROOT,
        app_script=app_script or _DEFAULT_DEPLOYMENT_APP_SCRIPT,
        python_executable=python_executable or sys.executable,
    )
    return adapter.probe()


def broker_provider_statuses(
    *,
    github_repository: str | None = None,
    github_executable: str = "gh",
    pypi_project_name: str | None = None,
    pypi_executable: str = "uv",
    deployment_app_script: Path | None = None,
    deployment_artifacts_root: Path | None = None,
    deployment_state_root: Path | None = None,
    deployment_python_executable: str | None = None,
) -> tuple[AdapterStatus, ...]:
    """Return the Stage 2 release-broker and Stage 3 deployment-adapter
    configuration status, deriving GitHub/PyPI identity from installed
    package metadata when not given explicitly. Performs no network call."""
    return (
        _github_release_broker_status(repository=github_repository, executable=github_executable),
        _pypi_release_broker_status(project_name=pypi_project_name, executable=pypi_executable),
        _local_service_deployment_status(
            app_script=deployment_app_script,
            artifacts_root=deployment_artifacts_root,
            state_root=deployment_state_root,
            python_executable=deployment_python_executable,
        ),
    )


__all__ = ["broker_provider_statuses"]
