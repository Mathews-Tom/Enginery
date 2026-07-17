#!/usr/bin/env python3
"""Render ``enginery.ledger.schema.MIGRATIONS`` into ``docs/ledger-schema.md``.

Documentation is generated from the migration list itself rather than
hand-maintained, so the schema reference in the repository cannot drift
from the code that actually defines the schema. Run with ``--check`` to
verify the committed file is up to date without rewriting it (useful as a
local pre-commit or CI guard).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from enginery.ledger.schema import MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "ledger-schema.md"


def render() -> str:
    lines = [
        "# Ledger schema",
        "",
        "Generated from `src/enginery/ledger/schema.py` by "
        "`scripts/generate_ledger_schema_doc.py`. Do not edit by hand — "
        "regenerate after changing `MIGRATIONS`.",
        "",
        "Migrations are forward-only. A schema mistake is corrected by a new "
        "migration appended to the list, never by editing an already-applied one.",
        "",
    ]
    for migration in MIGRATIONS:
        lines.append(f"## Migration {migration.version}: {migration.description}")
        lines.append("")
        for statement in migration.statements:
            lines.append("```sql")
            lines.append(statement.strip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify docs/ledger-schema.md is up to date instead of rewriting it",
    )
    args = parser.parse_args(argv)

    rendered = render()
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.is_file() else None
        if current != rendered:
            print(
                f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is stale; "
                "run scripts/generate_ledger_schema_doc.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
