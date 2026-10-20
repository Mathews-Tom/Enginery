"""Digest-registered, role-owned bootstrap calibration cases.

The fault digest identifies the exact deliberate-fault executable used by the
behavioral calibration driver, including for a deliberately withheld injection.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

CALIBRATION_SET_SHA256 = "d4cb1a7316581df48adef9770c8b8f5eb0d11af6004c4ede545b2f89e1be8690"


def load_calibration_set() -> dict[str, Any]:
    """Read only the registered revision; changing its bytes requires review."""
    content = files("enginery.evidence").joinpath("bootstrap_calibration_v1.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != CALIBRATION_SET_SHA256:
        raise ValueError("bootstrap calibration set digest mismatch")
    payload: dict[str, Any] = json.loads(content)
    if payload.get("schema_version") != 1 or len(payload.get("entries", [])) < 4:
        raise ValueError("invalid bootstrap calibration set")
    return payload
