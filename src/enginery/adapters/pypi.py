"""PyPI-compatible publication adapter using ``uv publish`` and the public JSON API.

Never reads or references the actual credential value: ``uv publish``
reads its token/username/password directly from environment variables
(``UV_PUBLISH_TOKEN`` or ``UV_PUBLISH_USERNAME``/``UV_PUBLISH_PASSWORD``)
that the subprocess inherits from its parent process. This module's own
Python code never touches that value -- it constructs a fixed argument
vector naming only the destination URL (never a secret) and lets ``uv``
resolve credentials internally. Ensuring the correct token is present in
the environment for the intended destination (PyPI or TestPyPI) is an
operator responsibility outside agent-authored code, never this
adapter's.

Destination verification uses PyPI's public, unauthenticated JSON API
(``GET {json_api_base}/{project}/{version}/json``) -- no credentials are
needed or used to confirm a publication landed with the expected digest.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.delivery_ports import PublicationReceipt, PublicationRequest
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    InvalidInputError,
    TransientProviderFailureError,
)
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

CommandRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
UrlOpener = Callable[[str], bytes]


def _default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _default_url_opener(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        data: bytes = response.read()
    return data


@dataclass(frozen=True, slots=True)
class PyPiAdapterConfig:
    """Opaque, credential-free destination configuration for one PyPI-compatible index."""

    project_name: str
    index_url: str
    publish_url: str
    json_api_base: str
    executable: str = "uv"

    def __post_init__(self) -> None:
        if not self.project_name.strip():
            raise InvalidInputError("project_name must be non-blank")
        for field_name, value in (
            ("index_url", self.index_url),
            ("publish_url", self.publish_url),
            ("json_api_base", self.json_api_base),
        ):
            if not value.startswith("https://"):
                raise InvalidInputError(f"{field_name} must be an https URL")


@dataclass(slots=True)
class PyPiAdapter:
    """Publishes and verifies fixture wheel/sdist artifacts against a PyPI-compatible index."""

    config: PyPiAdapterConfig
    command_runner: CommandRunner = field(default=_default_runner)
    url_opener: UrlOpener = field(default=_default_url_opener)
    _staged: dict[str, Path] = field(default_factory=dict, init=False)
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        try:
            result = self.command_runner((self.config.executable, "--version"), Path.cwd())
        except OSError:
            return AdapterStatus(
                kind=ProviderKind.RELEASE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="uv is not available",
            )
        if result.returncode != 0:
            return AdapterStatus(
                kind=ProviderKind.RELEASE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="uv is not available",
            )
        return AdapterStatus(
            kind=ProviderKind.RELEASE,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="pypi",
                provider_version=result.stdout.strip() or "unknown",
                api_version=ADAPTER_API_VERSION,
                capabilities=(AdapterCapability("publish_and_verify", 1),),
            ),
            detail="PyPI publication adapter is available",
        )

    def stage(self, *paths: Path) -> None:
        """Record which local built files correspond to which content digests before publish()."""
        for path in paths:
            digest = Digest.of_bytes(path.read_bytes())
            self._staged[str(digest)] = path

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        path = self._staged.get(str(request.artifact.digest))
        if path is None:
            raise InvalidInputError(
                "no staged file matches this artifact's digest; call stage() first"
            )
        result = self.command_runner(
            (
                self.config.executable,
                "publish",
                "--publish-url",
                self.config.publish_url,
                "--check-url",
                self.config.index_url,
                str(path),
            ),
            path.parent,
        )
        if result.returncode != 0:
            raise ExternalConflictError(
                "PyPI publish failed", details={"stderr": result.stderr[-2000:]}
            )
        self._outcomes[str(request.operation_id)] = ReconciliationResult.FOUND_MATCHING
        return PublicationReceipt(
            destination=request.destination,
            version=request.artifact.version,
            artifact_digest=request.artifact.digest,
        )

    def verify(self, receipt: PublicationReceipt) -> PublicationReceipt:
        payload = self._fetch_project_version(receipt.version)
        if payload is None:
            raise ExternalConflictError(
                "PyPI does not yet report this version", details={"version": receipt.version}
            )
        expected = receipt.artifact_digest.hex_value
        matching = [entry for entry in payload if self._sha256_digest(entry) == expected]
        if not matching:
            raise ExternalConflictError(
                "PyPI does not report a file matching the expected artifact digest",
                details={"version": receipt.version},
            )
        return receipt

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)

    @staticmethod
    def _sha256_digest(entry: Mapping[str, object]) -> str | None:
        digests = entry.get("digests")
        if not isinstance(digests, Mapping):
            return None
        value = digests.get("sha256")
        return value if isinstance(value, str) else None

    def _fetch_project_version(self, version: str) -> Sequence[Mapping[str, object]] | None:
        url = f"{self.config.json_api_base}/{self.config.project_name}/{version}/json"
        try:
            raw = self.url_opener(url)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise TransientProviderFailureError(
                "PyPI JSON API request failed", details={"status": error.code}
            ) from error
        except urllib.error.URLError as error:
            raise TransientProviderFailureError("PyPI JSON API request failed") from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise TransientProviderFailureError("PyPI JSON API returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("PyPI JSON API response must be a JSON object")
        urls = payload.get("urls")
        if not isinstance(urls, list):
            raise TransientProviderFailureError("PyPI JSON API 'urls' must be an array")
        return [entry for entry in urls if isinstance(entry, Mapping)]


__all__ = ["PyPiAdapter", "PyPiAdapterConfig"]
