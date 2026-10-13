"""The auth-custody receipt: where model credentials live and who may hold them.

Isolated workers never hold a provider credential. A model gateway and an auth broker on the
operator's macOS host hold the provider grants; a relay owned by the operator holds the gateway
bearer; a worker holds a dummy credential only. This module records that arrangement for one
host as a retained, canonical, digestable receipt: the grant scope, the endpoints, the custody
location, the allowed exposure, how credentials are refreshed, revoked and cleaned up, which
owner decisions authorise it, and *metadata only* about the gateway token file (whether it
exists, whether it is a regular file, its mode and its size).

The receipt NEVER contains the token, any part of it, or a hash of it: the builder is given file
metadata, never file content, and endpoints that carry credentials are refused.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

CUSTODY_RECEIPT_SCHEMA_VERSION = 1

RELAY_PROVIDERS = ("anthropic", "openai-codex")
"""Providers a worker can reach through the relay allowlist, in the owner's order."""

BROKER_ONLY_PROVIDERS = ("google-antigravity",)
"""Providers the broker holds a grant for but the relay denies: no worker can reach them."""

DEFAULT_DECISIONS = (
    "2026-10-13: auth-custody design approved",
    "2026-10-13: worker-readable gateway token accepted",
    "2026-10-13: relay allowlist anthropic+openai-codex ordered",
    "2026-10-13: Lima Ubuntu second host approved",
)
"""Owner decisions recorded when none are supplied (date: label; no secrets)."""

_MAX_DECISION_CHARS = 200
_MAX_TEXT_CHARS = 1024
_MODE_PATTERN = re.compile(r"^[0-7]{4}$")
_OWNER_ONLY_MODE = "0600"
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class CustodyHost(enum.Enum):
    """The host a receipt describes."""

    MACOS = "macos"
    UBUNTU_LIMA = "ubuntu-lima"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A credential-free http(s) endpoint."""

    url: str
    scheme: str
    host: str
    port: int | None

    @classmethod
    def parse(cls, url: str, *, name: str) -> Endpoint:
        """Parse ``url``, refusing userinfo, query strings and fragments (credential carriers)."""
        if not isinstance(url, str) or not url.strip() or url != url.strip():
            raise InvalidInputError(f"{name} must be a non-blank URL")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise InvalidInputError(f"{name} is not a valid URL") from error
        if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
            raise InvalidInputError(f"{name} must be an http(s) URL with a host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise InvalidInputError(f"{name} must not carry credentials (userinfo)")
        if parts.query or parts.fragment or "?" in url or "#" in url:
            raise InvalidInputError(f"{name} must not carry a query, fragment or credential")
        return cls(url=url, scheme=parts.scheme, host=parts.hostname, port=port)

    def to_state(self) -> dict[str, object]:
        return {"url": self.url, "scheme": self.scheme, "host": self.host, "port": self.port}

    @classmethod
    def from_state(cls, state: object, *, name: str) -> Endpoint:
        data = _mapping(state, name)
        endpoint = cls.parse(_text(data, "url", name), name=name)
        if data != endpoint.to_state():
            raise InvalidInputError(f"{name} fields do not match its url")
        return endpoint


@dataclass(frozen=True, slots=True)
class TokenFileMetadata:
    """What is known about the gateway token file WITHOUT reading it."""

    path: str
    exists: bool
    regular_file: bool
    symlink: bool
    mode: str | None
    size_bytes: int | None

    def __post_init__(self) -> None:
        _require_text(self.path, "token file path")
        if self.mode is not None and not _MODE_PATTERN.fullmatch(self.mode):
            raise InvalidInputError("token file mode must be four octal digits")
        if self.exists != (self.mode is not None) or self.exists != (self.size_bytes is not None):
            raise InvalidInputError("token file mode and size are present exactly when it exists")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise InvalidInputError("token file size cannot be negative")
        if self.regular_file and not self.exists:
            raise InvalidInputError("a missing token file cannot be a regular file")

    @property
    def owner_only(self) -> bool:
        return self.mode == _OWNER_ONLY_MODE

    def to_state(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "regular_file": self.regular_file,
            "symlink": self.symlink,
            "mode": self.mode,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_state(cls, state: object) -> TokenFileMetadata:
        data = _mapping(state, "token file")
        _exact_keys(data, ("path", "exists", "regular_file", "symlink", "mode", "size_bytes"))
        mode = data["mode"]
        size = data["size_bytes"]
        if mode is not None and not isinstance(mode, str):
            raise InvalidInputError("token file mode must be null or text")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool)):
            raise InvalidInputError("token file size must be null or an integer")
        return cls(
            path=_text(data, "path", "token file"),
            exists=_boolean(data, "exists", "token file"),
            regular_file=_boolean(data, "regular_file", "token file"),
            symlink=_boolean(data, "symlink", "token file"),
            mode=mode,
            size_bytes=size,
        )


@dataclass(frozen=True, slots=True)
class AuthCustodyReceipt:
    """One host's retained proof of the model-credential custody arrangement."""

    host: CustodyHost
    created_at: str
    gateway: Endpoint
    broker: Endpoint
    token_file: TokenFileMetadata
    relay_providers: tuple[str, ...]
    broker_only_providers: tuple[str, ...]
    custody_location: str
    custody: str
    exposure: tuple[str, ...]
    refresh: str
    revocation: tuple[str, ...]
    cleanup: tuple[str, ...]
    decisions: tuple[str, ...]
    findings: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("custody location", self.custody_location),
            ("custody", self.custody),
            ("refresh", self.refresh),
        ):
            _require_text(value, name)
        if not self.relay_providers or set(self.relay_providers) & set(self.broker_only_providers):
            raise InvalidInputError("relay and broker-only providers must be distinct and set")
        for name, values in (
            ("exposure", self.exposure),
            ("revocation", self.revocation),
            ("cleanup", self.cleanup),
            ("decisions", self.decisions),
            ("findings", self.findings),
        ):
            for value in values:
                _require_text(value, f"{name} entry")
        if not self.decisions:
            raise InvalidInputError("a custody receipt must reference at least one owner decision")

    @property
    def conforms(self) -> bool:
        """True when the observed token file matches what the receipt says about custody."""
        return not self.findings

    @property
    def digest(self) -> Digest:
        """The sha256 of the canonical JSON of :meth:`to_state`."""
        return Digest.of_json(self.to_state())

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": CUSTODY_RECEIPT_SCHEMA_VERSION,
            "host": self.host.value,
            "created_at": self.created_at,
            "grant_scope": {
                "relay_providers": list(self.relay_providers),
                "broker_only_providers": list(self.broker_only_providers),
            },
            "endpoints": {"gateway": self.gateway.to_state(), "broker": self.broker.to_state()},
            "custody": {
                "location": self.custody_location,
                "description": self.custody,
                "token_file": self.token_file.to_state(),
            },
            "exposure": list(self.exposure),
            "refresh": self.refresh,
            "revocation": list(self.revocation),
            "cleanup": list(self.cleanup),
            "owner_decisions": list(self.decisions),
            "findings": list(self.findings),
            "conforms": self.conforms,
        }

    @classmethod
    def from_state(cls, state: object) -> AuthCustodyReceipt:
        data = _mapping(state, "custody receipt")
        _exact_keys(
            data,
            (
                "schema_version",
                "host",
                "created_at",
                "grant_scope",
                "endpoints",
                "custody",
                "exposure",
                "refresh",
                "revocation",
                "cleanup",
                "owner_decisions",
                "findings",
                "conforms",
            ),
        )
        if data["schema_version"] != CUSTODY_RECEIPT_SCHEMA_VERSION:
            raise InvalidInputError("custody receipt schema version is unsupported")
        try:
            host = CustodyHost(data["host"])
        except ValueError as error:
            raise InvalidInputError("custody receipt host is unsupported") from error
        scope = _mapping(data["grant_scope"], "custody grant scope")
        endpoints = _mapping(data["endpoints"], "custody endpoints")
        custody = _mapping(data["custody"], "custody")
        receipt = cls(
            host=host,
            created_at=_text(data, "created_at", "custody receipt"),
            gateway=Endpoint.from_state(endpoints.get("gateway"), name="gateway endpoint"),
            broker=Endpoint.from_state(endpoints.get("broker"), name="broker endpoint"),
            token_file=TokenFileMetadata.from_state(custody.get("token_file")),
            relay_providers=_texts(scope, "relay_providers", "custody grant scope"),
            broker_only_providers=_texts(scope, "broker_only_providers", "custody grant scope"),
            custody_location=_text(custody, "location", "custody"),
            custody=_text(custody, "description", "custody"),
            exposure=_texts(data, "exposure", "custody receipt"),
            refresh=_text(data, "refresh", "custody receipt"),
            revocation=_texts(data, "revocation", "custody receipt"),
            cleanup=_texts(data, "cleanup", "custody receipt"),
            decisions=_texts(data, "owner_decisions", "custody receipt"),
            findings=_texts(data, "findings", "custody receipt"),
        )
        if data["conforms"] != receipt.conforms:
            raise InvalidInputError("custody receipt conformance does not match its findings")
        return receipt


def build_custody_receipt(
    *,
    host: CustodyHost,
    gateway_url: str,
    broker_url: str,
    token_file: TokenFileMetadata,
    created_at: datetime,
    decisions: Iterable[str] = (),
) -> AuthCustodyReceipt:
    """Build one host's custody receipt from endpoints, token-file metadata and decisions.

    ``decisions`` default to :data:`DEFAULT_DECISIONS`. A token file that is missing or not a
    regular file is a finding on every host; on ``ubuntu-lima`` the VM's own token copy must
    additionally be owner-only (mode 0600). The receipt records findings rather than hiding
    them, so a non-conforming host yields a receipt that says so.
    """
    if created_at.tzinfo is None:
        raise InvalidInputError("custody receipt time must be timezone-aware")
    gateway = Endpoint.parse(gateway_url, name="gateway url")
    broker = Endpoint.parse(broker_url, name="broker url")
    recorded = tuple(decisions) or DEFAULT_DECISIONS
    for decision in recorded:
        _require_decision(decision)
    path = token_file.path
    exposure = (
        "the relay holds the gateway bearer token",
        "workers hold only the dummy credential and can reach models only through the relay",
        "no worker, image, or run request carries a provider credential or refresh token",
    )
    if host is CustodyHost.MACOS:
        location = "macos-host"
        custody = (
            "the gateway and the auth broker run on the macOS host, which holds every provider "
            f"grant; the gateway token file is {path} on the macOS host"
        )
        revocation = (
            "omp auth-gateway token --regenerate (invalidates every copy of the bearer)",
            "restart the gateway so it only accepts the regenerated token",
        )
        cleanup = (
            "stop the relay: enginery runner relay down",
            "regenerate the gateway token: omp auth-gateway token --regenerate",
            f"delete the token file at {path} on the macOS host if it is no longer needed",
        )
    else:
        location = "remote-through-macos-gateway"
        custody = (
            "custody stays on the macOS host: the VM reaches the macOS gateway through "
            f"host.lima.internal and holds only a token copy at {path} (mode 0600)"
        )
        revocation = (
            "on the macOS host: omp auth-gateway token --regenerate (invalidates the VM's copy)",
            "restart the macOS gateway so it only accepts the regenerated token",
        )
        cleanup = (
            "stop the relay in the VM: enginery runner relay down",
            "regenerate the gateway token on the macOS host: omp auth-gateway token --regenerate",
            f"remove the VM token copy at {path}",
        )
    return AuthCustodyReceipt(
        host=host,
        created_at=created_at.isoformat(),
        gateway=gateway,
        broker=broker,
        token_file=token_file,
        relay_providers=RELAY_PROVIDERS,
        broker_only_providers=BROKER_ONLY_PROVIDERS,
        custody_location=location,
        custody=custody,
        exposure=exposure,
        refresh=(
            "the broker on the macOS host refreshes provider credentials; the relay and "
            "workers never hold a refresh token"
        ),
        revocation=revocation,
        cleanup=cleanup,
        decisions=recorded,
        findings=_findings(host, token_file),
    )


def _findings(host: CustodyHost, token_file: TokenFileMetadata) -> tuple[str, ...]:
    if not token_file.exists:
        return (f"the gateway token file {token_file.path} does not exist",)
    findings: list[str] = []
    if not token_file.regular_file:
        findings.append(f"the gateway token file {token_file.path} is not a regular file")
    if host is CustodyHost.UBUNTU_LIMA and not token_file.owner_only:
        findings.append(
            f"the VM token copy {token_file.path} has mode {token_file.mode}, expected 0600"
        )
    return tuple(findings)


def _require_decision(value: str) -> None:
    _require_text(value, "decision")
    if len(value) > _MAX_DECISION_CHARS or "\n" in value or "\r" in value:
        raise InvalidInputError(
            f"a decision must be one line of at most {_MAX_DECISION_CHARS} characters"
        )


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_CHARS:
        raise InvalidInputError(f"{name} must be non-blank text of at most {_MAX_TEXT_CHARS}")


def _mapping(state: object, name: str) -> Mapping[str, object]:
    if not isinstance(state, Mapping):
        raise InvalidInputError(f"{name} must be a mapping")
    return state


def _exact_keys(data: Mapping[str, object], keys: tuple[str, ...]) -> None:
    if set(data) != set(keys):
        raise InvalidInputError("custody receipt fields are not the expected set")


def _text(state: Mapping[str, object], key: str, name: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{name} {key} must be non-blank text")
    return value


def _boolean(state: Mapping[str, object], key: str, name: str) -> bool:
    value = state.get(key)
    if not isinstance(value, bool):
        raise InvalidInputError(f"{name} {key} must be a boolean")
    return value


def _texts(state: Mapping[str, object], key: str, name: str) -> tuple[str, ...]:
    value = state.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidInputError(f"{name} {key} must be a list of text")
    return tuple(value)


__all__ = [
    "BROKER_ONLY_PROVIDERS",
    "CUSTODY_RECEIPT_SCHEMA_VERSION",
    "DEFAULT_DECISIONS",
    "RELAY_PROVIDERS",
    "AuthCustodyReceipt",
    "CustodyHost",
    "Endpoint",
    "TokenFileMetadata",
    "build_custody_receipt",
]
