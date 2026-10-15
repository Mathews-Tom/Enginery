"""A small, bounded validator for the JSON-schema subset command packages may declare.

A command package declares an ``input_schema`` its stdin payload must satisfy
before anything is executed. Only this documented subset is accepted, so
schema validation is cheap, deterministic, and cannot be turned into a
resource-exhaustion vector; any other keyword is rejected when the manifest
is read rather than silently ignored.

Supported keywords:

* ``type``: ``object``, ``array``, ``string``, ``integer``, ``number``,
  ``boolean``, ``null``, or a non-empty list of those;
* ``enum`` and ``const``;
* ``properties``, ``required``, and ``additionalProperties`` (boolean only);
* ``items`` (a single schema);
* ``minimum`` and ``maximum`` for numbers;
* ``minLength`` and ``maxLength`` for strings;
* ``minItems`` and ``maxItems`` for arrays;
* annotations ``title``, ``description``, and ``$comment`` (ignored).

There is no ``$ref``, ``pattern``, ``format``, ``allOf``/``anyOf``/``oneOf``,
or remote or recursive reference of any kind.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from enginery.capabilities.errors import CapabilityInputSchemaError, CapabilityManifestError

MAX_SCHEMA_BYTES = 16 * 1024
MAX_SCHEMA_DEPTH = 8
MAX_INSTANCE_BYTES = 64 * 1024
MAX_INSTANCE_DEPTH = 16

_ANNOTATIONS = frozenset({"title", "description", "$comment"})
_KEYWORDS = _ANNOTATIONS | frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def check_schema(schema: object) -> None:
    """Reject a schema outside the supported subset; raises ``CapabilityManifestError``."""

    try:
        encoded = json.dumps(schema, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise CapabilityManifestError("input schema is not JSON-serializable") from error
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise CapabilityManifestError(
            "input schema is too large", details={"limit": MAX_SCHEMA_BYTES}
        )
    _check_node(schema, "$", depth=1)


def _check_node(node: object, path: str, *, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise CapabilityManifestError(
            "input schema is nested too deeply", details={"path": path, "limit": MAX_SCHEMA_DEPTH}
        )
    if not isinstance(node, dict):
        raise CapabilityManifestError("input schema nodes must be objects", details={"path": path})
    unsupported = sorted(set(node) - _KEYWORDS)
    if unsupported:
        raise CapabilityManifestError(
            "input schema uses unsupported keywords",
            details={"path": path, "keywords": unsupported},
        )
    _check_type(node.get("type"), path)
    _check_structure(node, path, depth=depth)
    _check_bounds(node, path)
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
        raise CapabilityManifestError(
            "input schema enum must be a non-empty list", details={"path": path}
        )


def _check_type(declared: object, path: str) -> None:
    if declared is None:
        return
    names = declared if isinstance(declared, list) else [declared]
    if not names or any(not isinstance(name, str) or name not in _TYPES for name in names):
        raise CapabilityManifestError("input schema type is not supported", details={"path": path})


def _check_structure(node: dict[str, object], path: str, *, depth: int) -> None:
    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise CapabilityManifestError(
                "input schema properties must be an object", details={"path": path}
            )
        for name, child in properties.items():
            _check_node(child, f"{path}.{name}", depth=depth + 1)
    if "items" in node:
        _check_node(node["items"], f"{path}[]", depth=depth + 1)
    required = node.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(name, str) for name in required)
    ):
        raise CapabilityManifestError(
            "input schema required must be a list of names", details={"path": path}
        )
    if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
        raise CapabilityManifestError(
            "input schema additionalProperties must be a boolean", details={"path": path}
        )


def _check_bounds(node: dict[str, object], path: str) -> None:
    for keyword in ("minimum", "maximum"):
        value = node.get(keyword)
        if value is not None and (not _is_number(value)):
            raise CapabilityManifestError(
                f"input schema {keyword} must be a number", details={"path": path}
            )
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        value = node.get(keyword)
        if value is not None and not _is_non_negative_int(value):
            raise CapabilityManifestError(
                f"input schema {keyword} must be a non-negative integer", details={"path": path}
            )


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate_instance(schema: Mapping[str, object], instance: object) -> None:
    """Validate ``instance`` against an already-checked ``schema``.

    Raises ``CapabilityInputSchemaError`` carrying the JSON path of the first
    violation, or when the instance itself exceeds the size or depth bounds.
    """

    try:
        encoded = json.dumps(instance, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise CapabilityInputSchemaError("input is not JSON-serializable") from error
    if len(encoded.encode("utf-8")) > MAX_INSTANCE_BYTES:
        raise CapabilityInputSchemaError(
            "input is too large", details={"limit": MAX_INSTANCE_BYTES}
        )
    _validate(schema, instance, "$", depth=1)


def _violation(path: str, message: str) -> CapabilityInputSchemaError:
    return CapabilityInputSchemaError(
        f"input violates the schema: {message}", details={"path": path}
    )


def _validate(schema: Mapping[str, object], instance: object, path: str, *, depth: int) -> None:
    if depth > MAX_INSTANCE_DEPTH:
        raise _violation(path, "input is nested too deeply")
    _validate_type(schema.get("type"), instance, path)
    if "const" in schema and not _equal(schema["const"], instance):
        raise _violation(path, "value does not equal the required constant")
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_equal(option, instance) for option in enum):
        raise _violation(path, "value is not one of the allowed options")
    if isinstance(instance, dict):
        _validate_object(schema, instance, path, depth=depth)
    elif isinstance(instance, list):
        _validate_array(schema, instance, path, depth=depth)
    elif isinstance(instance, str):
        _validate_string(schema, instance, path)
    elif isinstance(instance, int | float) and not isinstance(instance, bool):
        _validate_number(schema, instance, path)


def _equal(left: object, right: object) -> bool:
    # JSON equality: ``True == 1`` in Python but not in JSON.
    return type(left) is type(right) and left == right


def _type_matches(name: str, instance: object) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    return _is_number(instance)


def _validate_type(declared: object, instance: object, path: str) -> None:
    if declared is None:
        return
    names = declared if isinstance(declared, list) else [declared]
    if not any(isinstance(name, str) and _type_matches(name, instance) for name in names):
        raise _violation(path, f"expected type {' or '.join(map(str, names))}")


def _validate_object(
    schema: Mapping[str, object], instance: dict[str, object], path: str, *, depth: int
) -> None:
    properties = schema.get("properties")
    declared = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    for name in required if isinstance(required, list) else []:
        if name not in instance:
            raise _violation(f"{path}.{name}", "required property is missing")
    if schema.get("additionalProperties") is False:
        for name in sorted(set(instance) - set(declared)):
            raise _violation(f"{path}.{name}", "additional property is not allowed")
    for name, child in declared.items():
        if name in instance:
            _validate(child, instance[name], f"{path}.{name}", depth=depth + 1)


def _validate_array(
    schema: Mapping[str, object], instance: list[object], path: str, *, depth: int
) -> None:
    minimum, maximum = schema.get("minItems"), schema.get("maxItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise _violation(path, f"array has fewer than {minimum} items")
    if isinstance(maximum, int) and len(instance) > maximum:
        raise _violation(path, f"array has more than {maximum} items")
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(instance):
            _validate(items, item, f"{path}[{index}]", depth=depth + 1)


def _validate_string(schema: Mapping[str, object], instance: str, path: str) -> None:
    minimum, maximum = schema.get("minLength"), schema.get("maxLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        raise _violation(path, f"string is shorter than {minimum} characters")
    if isinstance(maximum, int) and len(instance) > maximum:
        raise _violation(path, f"string is longer than {maximum} characters")


def _validate_number(schema: Mapping[str, object], value: float, path: str) -> None:
    minimum, maximum = schema.get("minimum"), schema.get("maximum")
    if isinstance(minimum, int | float) and not isinstance(minimum, bool) and value < minimum:
        raise _violation(path, f"number is below the minimum {minimum}")
    if isinstance(maximum, int | float) and not isinstance(maximum, bool) and value > maximum:
        raise _violation(path, f"number is above the maximum {maximum}")


__all__ = [
    "MAX_INSTANCE_BYTES",
    "MAX_INSTANCE_DEPTH",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "check_schema",
    "validate_instance",
]
