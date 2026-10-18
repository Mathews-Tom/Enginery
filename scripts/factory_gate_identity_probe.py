"""Prove an installed wheel's identity, including uv's hashless archive_info receipts.

Run this module by absolute path with the installed entry point's interpreter and -I.
RECORD comparison verifies both the wheel payload and the installed payload, rather
than trusting a filename, direct_url URL, or an unverified installed RECORD alone.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def _records(text: str) -> dict[str, tuple[str, str]]:
    result = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 3 or not row[0] or row[0] in result:
            raise ValueError("invalid or duplicate RECORD entry")
        result[row[0]] = (row[1], row[2])
    return result


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and "\x00" not in name
        and str(path) == name
    )


def _verify_payload(payload: bytes, record: tuple[str, str]) -> None:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    if record != (f"sha256={digest}", str(len(payload))):
        raise ValueError("wheel or installed payload does not match RECORD")


def verify_records(wheel: Path, distribution: metadata.Distribution) -> str:
    installed = _records(distribution.read_text("RECORD") or "")
    with zipfile.ZipFile(wheel) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        names = {item.filename for item in members}
        if len(names) != len(members) or any(not _safe_name(name) for name in names):
            raise ValueError("wheel has unsafe or duplicate member paths")
        if any(stat.S_ISLNK(item.external_attr >> 16) for item in members):
            raise ValueError("wheel contains a symlink")
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise ValueError("wheel must contain exactly one distribution RECORD")
        record_name = record_names[0]
        record_bytes = archive.read(record_name)
        expected = _records(record_bytes.decode("utf-8"))
        if set(expected) != names or expected.get(record_name) != ("", ""):
            raise ValueError("wheel RECORD does not cover exactly its payload")
        if record_name not in installed:
            raise ValueError("installed distribution metadata differs from the wheel")
        root = Path(distribution.locate_file("")).resolve()
        for name, record in expected.items():
            path = Path(distribution.locate_file(name))
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                raise ValueError("installed wheel path escapes its distribution root")
            if name == record_name:
                continue
            if installed.get(name) != record:
                raise ValueError("installed RECORD differs from the bound wheel")
            _verify_payload(archive.read(name), record)
            _verify_payload(path.read_bytes(), record)
        metadata_dir = str(PurePosixPath(record_name).parent)
        generated = {
            f"{metadata_dir}/{name}"
            for name in ("INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json")
        }
        for name in installed.keys() - expected.keys() - generated:
            path = PurePosixPath(name)
            source = path.parent.parent / (path.name.split(".")[0] + ".py")
            bytecode = (
                _safe_name(name)
                and path.parent.name == "__pycache__"
                and re.fullmatch(r"[^.]+\.[^.]+(?:\.opt-[0-9]+)?\.pyc", path.name)
                and str(source) in expected
            )
            entry_point = (
                path.name == "enginery"
                and Path(distribution.locate_file(name)).resolve()
                == Path(sys.prefix).resolve() / "bin" / "enginery"
            )
            if not bytecode and not entry_point:
                raise ValueError("installed RECORD contains an unexpected payload")
    return hashlib.sha256(record_bytes).hexdigest()


def installed_identity(wheel: Path) -> dict[str, Any]:
    distribution = metadata.distribution("enginery")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    archive_info = direct_url.get("archive_info", {})
    installed_hash = archive_info.get("hashes", {}).get("sha256")
    legacy_hash = archive_info.get("hash")
    if legacy_hash is not None and legacy_hash != f"sha256={wheel_digest}":
        raise ValueError("installed archive hash differs from the bound wheel")
    if installed_hash is not None and installed_hash != wheel_digest:
        raise ValueError("installed archive hash differs from the bound wheel")
    record_digest = verify_records(wheel, distribution)
    return {
        "version": distribution.version,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "direct_url": direct_url,
        "wheel_record_sha256": record_digest,
    }


if __name__ == "__main__":
    print(json.dumps(installed_identity(Path(sys.argv[1]))))
