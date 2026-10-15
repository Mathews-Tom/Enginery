"""Count the words of the ``text`` field of the JSON document read from standard input."""

from __future__ import annotations

import json
import sys


def main() -> None:
    document = json.load(sys.stdin)
    json.dump({"words": len(str(document["text"]).split())}, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    main()
