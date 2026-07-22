from __future__ import annotations

from pathlib import Path

import pytest

from enginery.adapters.broker_doctor import (
    _github_release_broker_status,
    _local_service_deployment_status,
    _project_name_from_metadata,
    _pypi_release_broker_status,
    _repository_from_metadata,
    broker_provider_statuses,
)
from enginery.application.adapter_types import AdapterAvailability, ProviderKind

_APP_SCRIPT = (
    Path(__file__).resolve().parents[3] / "fixtures" / "enginery-stage3-local-service" / "app.py"
)


def test_repository_from_metadata_returns_none_for_an_unknown_distribution() -> None:
    assert _repository_from_metadata(distribution="no-such-enginery-fixture-distribution") is None


def test_repository_from_metadata_derives_the_real_enginery_repository() -> None:
    assert _repository_from_metadata() == "Mathews-Tom/Enginery"


def test_project_name_from_metadata_returns_none_for_an_unknown_distribution() -> None:
    assert _project_name_from_metadata(distribution="no-such-enginery-fixture-distribution") is None


def test_project_name_from_metadata_derives_the_real_enginery_project_name() -> None:
    assert _project_name_from_metadata() == "enginery"


def test_github_release_broker_status_falls_back_to_installed_metadata() -> None:
    status = _github_release_broker_status(repository=None, executable="true")

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.kind is ProviderKind.RELEASE
    assert status.fingerprint is not None
    assert status.fingerprint.provider_id == "github-release"


def test_github_release_broker_status_reports_unavailable_for_a_missing_executable() -> None:
    status = _github_release_broker_status(
        repository="owner/repo", executable="no-such-enginery-fixture-gh-xyz"
    )

    assert status.availability is AdapterAvailability.UNAVAILABLE
    assert status.fingerprint is None


def test_pypi_release_broker_status_falls_back_to_installed_metadata() -> None:
    status = _pypi_release_broker_status(project_name=None, executable="true")

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.kind is ProviderKind.RELEASE
    assert status.fingerprint is not None
    assert status.fingerprint.provider_id == "pypi"


def test_local_service_deployment_status_reports_misconfigured_with_nonexistent_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    status = _local_service_deployment_status(
        app_script=None,
        artifacts_root=None,
        state_root=None,
        python_executable=None,
    )

    assert status.availability is AdapterAvailability.MISCONFIGURED
    assert status.fingerprint is None
    assert "app_script" in status.detail


def test_local_service_deployment_status_reports_available_with_real_paths(tmp_path: Path) -> None:
    status = _local_service_deployment_status(
        app_script=_APP_SCRIPT,
        artifacts_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        python_executable=None,
    )

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.fingerprint is not None
    assert status.fingerprint.provider_id == "local-service-deployment"
    # probe() is diagnostic-only: it must not have created the directories.
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "state").exists()


def test_broker_provider_statuses_returns_release_and_deployment_entries(tmp_path: Path) -> None:
    statuses = broker_provider_statuses(
        github_repository="owner/repo",
        github_executable="true",
        pypi_project_name="fixture-project",
        pypi_executable="true",
        deployment_app_script=_APP_SCRIPT,
        deployment_artifacts_root=tmp_path / "artifacts",
        deployment_state_root=tmp_path / "state",
        deployment_python_executable=None,
    )

    assert [status.kind for status in statuses] == [
        ProviderKind.RELEASE,
        ProviderKind.RELEASE,
        ProviderKind.DEPLOYMENT,
    ]
    assert all(status.availability is AdapterAvailability.AVAILABLE for status in statuses)
    provider_ids = {status.fingerprint.provider_id for status in statuses if status.fingerprint}
    assert provider_ids == {"github-release", "pypi", "local-service-deployment"}
