"""Immutable, validated workflow manifests (03_SYSTEM_DESIGN.md §12).

A workflow manifest is repository-owned orchestration data: node graph,
typed schemas, retry/budget declarations, and terminal-state mapping. It
cannot embed arbitrary shell or a general programming language — every
field this package accepts is a closed, typed value, and the ``from_mapping``
parsers on ``NodeDeclaration`` and ``WorkflowManifest`` reject any key
outside their fixed schema.
"""

from __future__ import annotations
