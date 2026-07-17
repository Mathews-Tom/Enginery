#!/usr/bin/env python3
"""(Re)generate ``tests/fixtures/ledger.db``: a freshly migrated, healthy
ledger checked into the repository for ``enginery ledger verify`` to
exercise directly, without depending on any other test's setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "ledger.db"

from enginery.ledger.service import LedgerService  # noqa: E402
from enginery.ledger.verify import verify_ledger  # noqa: E402


def main() -> int:
    FIXTURE_PATH.unlink(missing_ok=True)
    LedgerService.open(FIXTURE_PATH).close()
    report = verify_ledger(FIXTURE_PATH)
    if not report.healthy:
        print(f"generated fixture failed verification: {report.issues}", file=sys.stderr)
        return 1
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)} (schema version {report.schema_version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
