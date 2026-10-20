"""Read-only bootstrap probes run by the bound installation's interpreter."""

from __future__ import annotations

import contextlib
import importlib.abc
import io
import json
import runpy
import sqlite3
import sys
from pathlib import Path
from typing import Any


def shared_reader() -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).with_name("factory_gate_project_probe.py")))


def operator(arguments: list[str], entry_point: str, local: bool) -> dict[str, Any]:
    class NoWorkflow(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
            if fullname == "enginery.workflows" or fullname.startswith("enginery.workflows."):
                raise RuntimeError("operator imported workflow code: " + fullname)

    sys.meta_path.insert(0, NoWorkflow())
    launches = []

    def audit(event: str, args: Any) -> None:
        if event != "subprocess.Popen":
            return
        argv = args[1]
        if not isinstance(argv, (list, tuple)):
            raise RuntimeError("operator attempted an opaque subprocess")
        argv = [str(item) for item in argv]
        name = Path(argv[0]).name
        launches.append(argv)
        allowed = name in ("ps", "ioreg", "sysctl")
        if name in ("docker", "podman"):
            tail = argv[1:]
            if tail[:1] == ["--context"]:
                tail = tail[2:]
            allowed = bool(tail) and (
                tail[0] in ("info", "version", "inspect", "ps", "events")
                or (
                    len(tail) > 1
                    and tail[0] in ("image", "network", "volume", "container")
                    and tail[1] == "inspect"
                )
            )
        if not allowed:
            raise RuntimeError(
                "operator attempted a worker/agent launch or non-read-only subprocess"
            )

    sys.addaudithook(audit)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if local:
                from types import SimpleNamespace

                from enginery.cli.operator import main
                from enginery.domain.runner import RunnerProfile

                image = arguments[arguments.index("--image") + 1] if "--image" in arguments else ""
                admission = SimpleNamespace(
                    profile=RunnerProfile(),
                    admit=lambda selection: SimpleNamespace(
                        hold=None,
                        engine=SimpleNamespace(to_state=lambda: {"kind": "deterministic-stand-in"}),
                        image=SimpleNamespace(image_id=image, to_state=lambda: {"image_id": image}),
                    ),
                )
                code = main(arguments[1:], admission=admission)
            else:
                sys.argv = [entry_point, *arguments]
                runpy.run_path(entry_point, run_name="__main__")
                code = 0
        except SystemExit as error:
            code = error.code
    bad = sorted(name for name in sys.modules if name.startswith("enginery.workflows"))
    if bad:
        raise RuntimeError("operator imported workflows: " + repr(bad))
    return {
        "code": code,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "workflow_imports": bad,
        "subprocesses": launches,
    }


def project(settings: dict[str, Any]) -> dict[str, Any]:
    from enginery.domain.behavioral_evidence import EvidenceReceipt
    from enginery.domain.digests import Digest
    from enginery.ledger.artifact_store import ArtifactStore

    result = shared_reader()["project_state"](
        settings["database"], settings["run_id"], settings["artifact_root"]
    )
    store = ArtifactStore(Path(settings["artifact_root"]), create_directories=False)
    references = list(settings.get("artifact_digests", []))
    behavior, calibration = result["behavior"], result["calibration"]
    references.extend(behavior.get("observation_receipt_digests", []))
    for value in (behavior.get("receipt_digest"), calibration.get("calibration_receipt_digest")):
        if value:
            references.append(value)
    receipts, verified = {}, []
    pending = list(dict.fromkeys(references))
    while pending:
        text = pending.pop()
        if text in verified:
            continue
        raw = store.read_bytes(Digest(*text.split(":", 1)))
        verified.append(text)
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if isinstance(document, dict) and "capture_manifest" in document:
            receipt = EvidenceReceipt.from_state(document)
            if str(receipt.digest) != text:
                raise RuntimeError("noncanonical behavioral receipt")
            for capture in receipt.capture_manifest.artifacts:
                if len(store.read_bytes(capture.digest)) != capture.size_bytes:
                    raise RuntimeError("capture size drift")
                verified.append(str(capture.digest))
            pending.extend(str(item) for item in receipt.input_receipt_digests)
            receipts[text] = document
    for item in store.iter_digests():
        store.read_bytes(item)
        verified.append(str(item))
    verified_stores = {}
    for key, bound in settings.get("campaign_stores", {}).items():
        other = ArtifactStore(Path(bound["artifact_root"]), create_directories=False)
        actual = []
        for item in other.iter_digests():
            other.read_bytes(item)
            actual.append(str(item))
        if not set(bound["artifact_digests"]).issubset(actual):
            raise ValueError("campaign product artifact missing: " + key)
        verified_stores[key] = sorted(actual)
    result.update(
        receipts=receipts, verified_artifacts=sorted(set(verified)), verified_stores=verified_stores
    )
    return dict(result)


def snapshot(database: str, artifacts: str) -> dict[str, Any]:
    from enginery.ledger.artifact_store import ArtifactStore
    from enginery.ledger.service import LedgerService

    connection = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    ledger = LedgerService(connection)
    try:
        projections = [
            (row["aggregate_type"], row["aggregate_id"], row["state_json"])
            for row in connection.execute(
                "SELECT aggregate_type, aggregate_id, state_json FROM projections "
                "ORDER BY aggregate_type, aggregate_id"
            )
            if row["aggregate_type"] != "operator_control"
        ]
        store = ArtifactStore(Path(artifacts), create_directories=False)
        digests = sorted(str(item) for item in store.iter_digests())
        for item in store.iter_digests():
            store.read_bytes(item)
        return {"projections": projections, "artifacts": digests}
    finally:
        ledger.close()


def main() -> None:
    command, *args = sys.argv[1:]
    if command == "operator":
        result = operator(json.loads(args[0]), args[1], args[2] == "local")
    elif command == "project":
        result = project(json.loads(args[0]))
    elif command == "compare":
        left, right = snapshot(*args[:2]), snapshot(*args[2:])
        result = {
            "equivalent": left == right,
            "artifact_count": len(left["artifacts"]),
            "projection_count": len(left["projections"]),
        }
    else:
        raise ValueError("unknown bootstrap probe: " + command)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
