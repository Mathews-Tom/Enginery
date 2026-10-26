#!/opt/enginery/runtime/bin/python3.12
"""Run the current checkout, without installing it or writing to its workspace."""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from collections.abc import Callable
from email.message import Message
from importlib import metadata
from os import PathLike
from pathlib import Path
from typing import cast


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class CheckoutDistribution(metadata.Distribution):
    """Supply distribution metadata from the candidate, not an installed Enginery."""

    def __init__(self, root: Path) -> None:
        self.root = root
        contents = (root / "pyproject.toml").read_text(encoding="utf-8")
        project = tomllib.loads(contents).get("project")
        if not isinstance(project, dict) or not all(
            isinstance(project.get(key), str) and project[key].strip()
            for key in ("name", "version")
        ):
            raise ValueError("missing project name or version")
        scripts = project.get("scripts")
        target = scripts.get("enginery") if isinstance(scripts, dict) else None
        if not isinstance(target, str) or ":" not in target:
            raise ValueError("missing console script in [project.scripts]")
        self.entry_point = target
        self.normalized_name = _normalized(project["name"])
        message = Message()
        message["Metadata-Version"] = "2.1"
        message["Name"] = project["name"]
        message["Version"] = project["version"]
        urls = project.get("urls")
        for label, url in urls.items() if isinstance(urls, dict) else ():
            message["Project-URL"] = f"{label}, {url}"
        self.text = message.as_string()

    def console_main(self) -> Callable[[], int]:
        """The console script the checkout itself declares in ``[project.scripts]``."""
        module_name, _, attribute = self.entry_point.partition(":")
        target: object = importlib.import_module(module_name.strip())
        for part in attribute.strip().split("."):
            target = getattr(target, part)
        if not callable(target):
            raise TypeError(f"entry point {self.entry_point!r} is not callable")
        return cast("Callable[[], int]", target)

    def read_text(self, filename: str) -> str | None:
        return self.text if filename == "METADATA" else None

    def locate_file(self, path: str | PathLike[str]) -> Path:
        return self.root / path


class CheckoutMetadataFinder(metadata.DistributionFinder):
    def __init__(self, distribution: CheckoutDistribution) -> None:
        self.distribution = distribution

    def find_distributions(
        self, context: metadata.DistributionFinder.Context | None = None
    ) -> tuple[metadata.Distribution, ...]:
        context = context or metadata.DistributionFinder.Context()
        if context.name is None or _normalized(context.name) == self.distribution.normalized_name:
            return (self.distribution,)
        return ()


def main() -> int:
    root = Path.cwd()
    source = root / "src"
    if not (source / "enginery").is_dir():
        print(
            "enginery-checkout: ./src/enginery is absent; run from an Enginery checkout",
            file=sys.stderr,
        )
        return 2
    try:
        distribution = CheckoutDistribution(root)
    except (OSError, ValueError):
        print(
            "enginery-checkout: ./pyproject.toml lacks a readable [project] name, version "
            "and console script",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(source))
    sys.meta_path.insert(0, CheckoutMetadataFinder(distribution))
    return distribution.console_main()()


if __name__ == "__main__":
    raise SystemExit(main())
