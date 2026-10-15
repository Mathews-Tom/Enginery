"""Capability locking, provenance classification, and immutable materialization.

This package resolves the capability names a workflow node requests into
an exact lock over complete package closures (every file, plus declared
dependency pins), classifies where each locked capability's trust comes
from, checks each package's license, and writes approved package files
into a workspace without ever mutating them in place. It never imports a
provider SDK or executes package content: a capability source reaches this
package only through the ``CapabilitySourcePort`` declared in
``enginery.application.delivery_ports``.
"""

from __future__ import annotations
