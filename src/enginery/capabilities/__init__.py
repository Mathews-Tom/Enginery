"""Capability locking, provenance classification, and immutable materialization.

This package resolves the capability names a workflow node requests into
an exact, content-addressed lock, classifies where each locked capability's
trust comes from, and writes approved capability bytes into a workspace
without ever mutating them in place. It never imports a provider SDK: a
capability source reaches this package only through the
``CapabilitySourcePort`` declared in ``enginery.application.delivery_ports``.
"""

from __future__ import annotations
