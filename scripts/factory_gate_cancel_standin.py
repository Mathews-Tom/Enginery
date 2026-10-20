"""Local no-provider harness process for cancellation qualification."""

from __future__ import annotations

import argparse
import json
import time


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--harness", choices=("omp", "claude"), required=True)
    parser.add_argument("--version", action="store_true")
    options, _ = parser.parse_known_args()
    if options.version:
        print("omp v18.10.3" if options.harness == "omp" else "2.1.296 (Claude Code)")
        return
    event = (
        {"type": "agent_start"}
        if options.harness == "omp"
        else {"type": "system", "subtype": "init"}
    )
    print(json.dumps(event), flush=True)
    time.sleep(900)


if __name__ == "__main__":
    main()
