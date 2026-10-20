"""Shared installed-entry-point and paginated GitHub read boundaries."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory_gate_context import GateHoldError


def bound_interpreter(executable: str | Path) -> str:
    with Path(executable).open(encoding="utf-8") as stream:
        shebang = stream.readline().strip()
    interpreter = shebang.removeprefix("#!")
    if not shebang.startswith("#!/") or any(character.isspace() for character in interpreter):
        raise GateHoldError("installed entry point must have an absolute virtualenv Python shebang")
    return interpreter


def pr_snapshot(
    repository: str, number: int, execute: Callable[[list[str]], Any]
) -> dict[str, Any]:
    if not isinstance(number, int) or number < 1:
        raise GateHoldError("product did not retain a real PR number")
    root = f"repos/{repository}"

    def get(endpoint: str, *, pages: bool = False) -> Any:
        return execute(
            [
                "gh",
                "api",
                "--method",
                "GET",
                *(["--paginate", "--slurp"] if pages else []),
                endpoint,
            ]
        )

    pr = get(f"{root}/pulls/{number}")
    files = get(f"{root}/pulls/{number}/files?per_page=100", pages=True)
    checks = get(f"{root}/commits/{pr['head']['sha']}/check-runs?per_page=100", pages=True)
    reviews = get(f"{root}/pulls/{number}/reviews?per_page=100", pages=True)
    pr.update(
        changed_paths=sorted(row["filename"] for page in files for row in page),
        check_runs=[row for page in checks for row in page["check_runs"]],
        reviews=[row for page in reviews for row in page],
    )
    latest = get(f"{root}/pulls/{number}")

    def identity(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row.get("state"),
            row.get("merged"),
            *(
                (row.get(side) or {}).get(field)
                for side in ("head", "base")
                for field in ("sha", "ref")
            ),
        )

    if identity(pr) != identity(latest):
        raise GateHoldError("PR changed during the fresh host snapshot")
    return pr
