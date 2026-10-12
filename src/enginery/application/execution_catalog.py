"""Load the digest-frozen native execution-control catalog shipped as package data.

Each catalog version file is verified against a SHA-256 registered here before
it is parsed, so an in-place edit of the file alone fails loudly. Changing a
registered digest, or adding a version, is a reviewable source change. Runs bind
the digest of the exact catalog they were resolved against; a later catalog
never silently rebinds them.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import ExecutionCatalog, catalog_from_mapping

_CONFIG_DIRECTORY = Path(__file__).parent.parent / "config"

CURRENT_CATALOG_VERSION = "1"

REGISTERED_CATALOG_DIGESTS: Mapping[str, str] = {
    "1": "23598e0c044269fddefc073583fddf2432d940626fad6130d2b9e64d805196a3",
}
"""SHA-256 of each frozen catalog file's bytes, keyed by ``catalog_version``."""


class ExecutionCatalogError(InvalidInputError):
    """Raised when a catalog file is malformed, tampered with, or unregistered."""


def load_execution_catalog(version: str = CURRENT_CATALOG_VERSION) -> ExecutionCatalog:
    """Load one registered catalog version, verifying its frozen digest first."""
    registered = REGISTERED_CATALOG_DIGESTS.get(version)
    if registered is None:
        raise ExecutionCatalogError(
            "execution catalog version is not registered", details={"catalog_version": version}
        )
    raw = (_CONFIG_DIRECTORY / f"execution-catalog-v{version}.toml").read_bytes()
    digest = Digest.of_bytes(raw)
    if digest.hex_value != registered:
        raise ExecutionCatalogError(
            "execution catalog file does not match its registered digest",
            details={
                "catalog_version": version,
                "registered_digest": registered,
                "observed_digest": digest.hex_value,
            },
        )
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ExecutionCatalogError("execution catalog file is not valid TOML") from error
    catalog = catalog_from_mapping(data, digest=digest)
    if catalog.version != version:
        raise ExecutionCatalogError(
            "execution catalog file declares a different version than its registration",
            details={"catalog_version": version, "declared_version": catalog.version},
        )
    return catalog


__all__ = [
    "CURRENT_CATALOG_VERSION",
    "REGISTERED_CATALOG_DIGESTS",
    "ExecutionCatalogError",
    "load_execution_catalog",
]
