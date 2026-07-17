#!/usr/bin/env python3
"""Enforce the modular-monolith package-boundary rules (03_SYSTEM_DESIGN.md §8.1).

Given a layer name, parse every module under ``src/enginery/<layer>`` with
``ast`` and reject any import of an ``enginery`` subpackage the layer is not
allowed to depend on. Import direction is inward: ``domain`` depends on
nothing else in this repository; ``cli`` and ``adapters`` may depend on
everything inward of them.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "enginery"
PACKAGE = "enginery"

# Allowed import direction between enginery.* layers (03_SYSTEM_DESIGN.md §8.1, §8).
LAYER_ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"domain", "application"}),
    "engine": frozenset({"domain", "application", "engine", "ledger", "policy", "evidence"}),
    "ledger": frozenset({"domain", "application", "ledger"}),
    "policy": frozenset({"domain", "application", "policy", "evidence"}),
    "evidence": frozenset({"domain", "application", "evidence"}),
    "evaluation": frozenset({"domain", "application", "evaluation", "ledger"}),
    "adapters": frozenset(
        {
            "domain",
            "application",
            "engine",
            "ledger",
            "policy",
            "evidence",
            "evaluation",
            "adapters",
        }
    ),
    "cli": frozenset(
        {
            "domain",
            "application",
            "engine",
            "ledger",
            "policy",
            "evidence",
            "evaluation",
            "adapters",
            "cli",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Violation:
    file: Path
    imported_module: str
    target_layer: str


def _iter_layer_modules(layer: str, *, src_root: Path) -> list[Path]:
    layer_root = src_root / layer
    if not layer_root.is_dir():
        raise SystemExit(f"unknown layer {layer!r}: {layer_root} does not exist")
    return sorted(layer_root.rglob("*.py"))


def _module_dotted_name(file: Path, *, src_root: Path) -> tuple[str, ...]:
    rel = file.relative_to(src_root.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return tuple(parts)


def _importing_package(file: Path, *, src_root: Path) -> tuple[str, ...]:
    dotted = _module_dotted_name(file, src_root=src_root)
    if file.name == "__init__.py":
        return dotted
    return dotted[:-1]


def _resolve_import_from_base(
    file: Path, node: ast.ImportFrom, *, src_root: Path
) -> tuple[str, ...]:
    if node.level == 0:
        return tuple(node.module.split(".")) if node.module else ()
    package_parts = list(_importing_package(file, src_root=src_root))
    strip = node.level - 1
    if strip:
        package_parts = package_parts[:-strip] if strip < len(package_parts) else []
    if node.module:
        return (*package_parts, *node.module.split("."))
    return tuple(package_parts)


def _candidate_modules_for_import_from(
    file: Path, node: ast.ImportFrom, *, src_root: Path
) -> list[str]:
    base_parts = _resolve_import_from_base(file, node, src_root=src_root)
    if not base_parts:
        return []
    candidates = [".".join(base_parts)]
    if base_parts == (PACKAGE,):
        # `from enginery import x, y` — each alias may itself be a layer package.
        candidates.extend(f"{PACKAGE}.{alias.name}" for alias in node.names)
    return candidates


def _target_layer(module: str) -> str | None:
    prefix = PACKAGE + "."
    if not module.startswith(prefix):
        return None
    return module[len(prefix) :].split(".")[0]


def find_violations(layer: str, *, src_root: Path = SRC_ROOT) -> list[Violation]:
    allowed = LAYER_ALLOWED_IMPORTS[layer]
    violations: list[Violation] = []
    for file in _iter_layer_modules(layer, src_root=src_root):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.extend(
                    _candidate_modules_for_import_from(file, node, src_root=src_root)
                )
            for module in imported_modules:
                target_layer = _target_layer(module)
                if target_layer is not None and target_layer not in allowed:
                    violations.append(
                        Violation(file=file, imported_module=module, target_layer=target_layer)
                    )
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Enginery package-boundary import rules.")
    parser.add_argument("layer", choices=sorted(LAYER_ALLOWED_IMPORTS))
    args = parser.parse_args(argv)

    violations = find_violations(args.layer)
    if violations:
        for violation in violations:
            rel = violation.file.relative_to(REPO_ROOT)
            print(
                f"BOUNDARY VIOLATION: {rel} imports {violation.imported_module!r} "
                f"(layer {violation.target_layer!r} not allowed from {args.layer!r})",
                file=sys.stderr,
            )
        return 1
    print(f"import boundaries verified for layer {args.layer!r}: 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
