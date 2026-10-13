"""A stand-in ``gh`` executable that serves one labelled GitHub issue from disk.

The script answers the two read endpoints ``GitHubWorkLedger.fetch`` uses -- the issue and
its labels -- for any ``owner/repo`` and issue number, from a JSON document beside it, and
appends every invocation to a log so a test can show which calls were made. It is passed to
the CLI as ``--github-executable``, so the real adapter, subprocess boundary and request
decoding all run.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

CANONICAL_LABELS = ("enginery/work-kind/issue", "enginery/risk/low")
ISSUE_TITLE = "Bounded change"
ISSUE_BODY = "Change one bounded behavior."
ISSUE_UPDATED_AT = "2026-10-13T00:00:00Z"

_SCRIPT = """#!__PYTHON__
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
state = json.loads((here / "gh-state.json").read_text(encoding="utf-8"))
arguments = sys.argv[1:]
with (here / "gh-calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
if arguments == ["--version"]:
    print("gh version 0.0.0 (stand-in)")
    raise SystemExit(0)
endpoint = arguments[-1] if arguments[:1] == ["api"] else ""
path, _, query = endpoint.partition("?")
parts = path.split("/")
if len(parts) in (5, 6) and parts[0] == "repos" and parts[3] == "issues" and parts[4].isdecimal():
    if len(parts) == 5:
        print(
            json.dumps(
                {
                    "url": "https://api.github.com/" + path,
                    "number": int(parts[4]),
                    "title": state["title"],
                    "body": state["body"],
                    "updated_at": state["updated_at"],
                }
            )
        )
        raise SystemExit(0)
    if parts[5] == "labels":
        page = int(dict(pair.split("=") for pair in query.split("&") if pair).get("page", "1"))
        print(json.dumps([{"name": name} for name in state["labels"]] if page == 1 else []))
        raise SystemExit(0)
sys.stderr.write("HTTP 404: Not Found\\n")
raise SystemExit(1)
"""


@dataclass(frozen=True, slots=True)
class StandInGitHub:
    """The on-disk pieces of one stand-in ``gh`` and the issue it currently serves."""

    executable: Path
    state_path: Path
    log_path: Path

    def serve(
        self,
        *,
        labels: tuple[str, ...] = CANONICAL_LABELS,
        title: str = ISSUE_TITLE,
        body: str | None = ISSUE_BODY,
        updated_at: str = ISSUE_UPDATED_AT,
    ) -> None:
        """Replace the issue every later invocation serves."""
        self.state_path.write_text(
            json.dumps(
                {"labels": list(labels), "title": title, "body": body, "updated_at": updated_at}
            ),
            encoding="utf-8",
        )

    def calls(self) -> list[list[str]]:
        """Every invocation so far, as its argument vector."""
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]


def write_stand_in_gh(directory: Path) -> StandInGitHub:
    """Write a stand-in ``gh`` into ``directory`` serving a canonically labelled issue."""
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "gh"
    executable.write_text(_SCRIPT.replace("__PYTHON__", sys.executable), encoding="utf-8")
    executable.chmod(0o755)
    stand_in = StandInGitHub(
        executable=executable,
        state_path=directory / "gh-state.json",
        log_path=directory / "gh-calls.jsonl",
    )
    stand_in.serve()
    return stand_in
