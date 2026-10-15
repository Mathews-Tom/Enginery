from __future__ import annotations

import pytest

from enginery.capabilities.errors import CapabilityInputSchemaError, CapabilityManifestError
from enginery.capabilities.schema import (
    MAX_INSTANCE_BYTES,
    MAX_INSTANCE_DEPTH,
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    check_schema,
    validate_instance,
)


def _declaration_problem(schema: object) -> CapabilityManifestError:
    with pytest.raises(CapabilityManifestError) as caught:
        check_schema(schema)
    return caught.value


def _violation_path(schema: dict[str, object], instance: object) -> object:
    with pytest.raises(CapabilityInputSchemaError) as caught:
        validate_instance(schema, instance)
    return caught.value.details.get("path")


def _nested_properties(levels: int) -> dict[str, object]:
    node: dict[str, object] = {"type": "object"}
    for _ in range(levels - 1):
        node = {"type": "object", "properties": {"a": node}}
    return node


def _nested_items(levels: int) -> dict[str, object]:
    node: dict[str, object] = {}
    for _ in range(levels):
        node = {"items": node}
    return node


def _nested_lists(levels: int) -> object:
    instance: object = 1
    for _ in range(levels):
        instance = [instance]
    return instance


# --- check_schema: the accepted subset ---------------------------------------------------------


def test_check_schema_accepts_every_documented_keyword() -> None:
    check_schema(
        {
            "title": "t",
            "description": "d",
            "$comment": "c",
            "type": "object",
            "required": ["name"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 10, "enum": ["a", "b"]},
                "count": {"type": ["integer", "null"], "minimum": 0, "maximum": 5.5},
                "flag": {"const": True},
                "tags": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 3,
                    "items": {"type": "string"},
                },
            },
        }
    )


def test_check_schema_accepts_an_empty_schema() -> None:
    check_schema({})


def test_check_schema_accepts_the_maximum_depth() -> None:
    check_schema(_nested_properties(MAX_SCHEMA_DEPTH))


# --- check_schema: rejections ------------------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    [
        "$ref",
        "pattern",
        "format",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "$defs",
        "definitions",
        "default",
        "patternProperties",
        "uniqueItems",
        "dependencies",
        "$schema",
        "totally-unknown",
    ],
)
def test_check_schema_rejects_unsupported_keywords(keyword: str) -> None:
    error = _declaration_problem({"type": "object", keyword: "x"})

    assert error.details["keywords"] == [keyword]
    assert error.details["path"] == "$"


def test_check_schema_rejects_an_unsupported_keyword_inside_a_property() -> None:
    error = _declaration_problem(
        {"type": "object", "properties": {"name": {"type": "string", "pattern": "^a"}}}
    )

    assert error.details["path"] == "$.name"
    assert error.details["keywords"] == ["pattern"]


def test_check_schema_rejects_an_unsupported_keyword_inside_items() -> None:
    error = _declaration_problem({"type": "array", "items": {"$ref": "#/x"}})

    assert error.details["path"] == "$[]"


@pytest.mark.parametrize("schema", [[], "object", True, None, 3])
def test_check_schema_rejects_a_non_object_root(schema: object) -> None:
    assert "must be objects" in str(_declaration_problem(schema))


def test_check_schema_rejects_a_non_object_property_schema() -> None:
    error = _declaration_problem({"properties": {"a": True}})

    assert error.details["path"] == "$.a"


def test_check_schema_rejects_a_schema_that_is_not_json() -> None:
    assert "JSON" in str(_declaration_problem({"const": object()}))


def test_check_schema_rejects_a_schema_nested_too_deeply() -> None:
    error = _declaration_problem(_nested_properties(MAX_SCHEMA_DEPTH + 1))

    assert error.details["limit"] == MAX_SCHEMA_DEPTH
    assert "too deeply" in str(error)


def test_check_schema_rejects_items_nested_too_deeply() -> None:
    error = _declaration_problem(_nested_items(MAX_SCHEMA_DEPTH))

    assert "too deeply" in str(error)


def test_check_schema_rejects_an_oversized_schema() -> None:
    error = _declaration_problem({"description": "x" * (MAX_SCHEMA_BYTES + 1)})

    assert error.details["limit"] == MAX_SCHEMA_BYTES
    assert "too large" in str(error)


@pytest.mark.parametrize("value", [{}, "no", 0, 1, None, []])
def test_check_schema_rejects_non_boolean_additional_properties(value: object) -> None:
    error = _declaration_problem({"additionalProperties": value})

    assert "additionalProperties" in str(error)


@pytest.mark.parametrize("required", ["name", [1], ["a", None], {"a": 1}])
def test_check_schema_rejects_a_malformed_required(required: object) -> None:
    assert "required" in str(_declaration_problem({"required": required}))


@pytest.mark.parametrize("declared", ["date", "Object", [], ["string", "date"], ["string", 5], 5])
def test_check_schema_rejects_an_unsupported_type(declared: object) -> None:
    assert "type" in str(_declaration_problem({"type": declared}))


@pytest.mark.parametrize("properties", [[], "x", ["a"]])
def test_check_schema_rejects_non_object_properties(properties: object) -> None:
    assert "properties" in str(_declaration_problem({"properties": properties}))


@pytest.mark.parametrize("enum", [[], "a", {"a": 1}])
def test_check_schema_rejects_a_bad_enum(enum: object) -> None:
    assert "enum" in str(_declaration_problem({"enum": enum}))


@pytest.mark.parametrize("keyword", ["minimum", "maximum"])
@pytest.mark.parametrize("value", ["1", True, [1]])
def test_check_schema_rejects_a_non_numeric_bound(keyword: str, value: object) -> None:
    assert keyword in str(_declaration_problem({keyword: value}))


@pytest.mark.parametrize("keyword", ["minLength", "maxLength", "minItems", "maxItems"])
@pytest.mark.parametrize("value", [-1, 1.5, "1", True])
def test_check_schema_rejects_a_bad_length_bound(keyword: str, value: object) -> None:
    assert keyword in str(_declaration_problem({keyword: value}))


# --- validate_instance: accepted instances -----------------------------------------------------


def test_validate_instance_accepts_a_conforming_document() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "required": ["name"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 5},
            "count": {"type": "integer", "minimum": 0, "maximum": 10},
            "ratio": {"type": "number"},
            "tags": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            "none": {"type": "null"},
            "flag": {"type": "boolean"},
        },
    }

    validate_instance(
        schema,
        {"name": "abc", "count": 10, "ratio": 0.5, "tags": ["a"], "none": None, "flag": False},
    )


def test_validate_instance_with_an_empty_schema_accepts_any_json() -> None:
    for instance in (None, 1, "x", [1, "a"], {"a": {"b": []}}):
        validate_instance({}, instance)


def test_validate_instance_ignores_undeclared_properties_unless_forbidden() -> None:
    validate_instance({"properties": {"a": {"type": "integer"}}}, {"a": 1, "other": "anything"})


def test_validate_instance_accepts_the_maximum_instance_depth() -> None:
    validate_instance(_nested_items(MAX_INSTANCE_DEPTH - 1), _nested_lists(MAX_INSTANCE_DEPTH - 1))


# --- validate_instance: types ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "instance"),
    [
        ("object", []),
        ("object", None),
        ("array", {}),
        ("string", 1),
        ("string", None),
        ("integer", "1"),
        ("integer", 1.5),
        ("number", "1"),
        ("boolean", "true"),
        ("boolean", 0),
        ("null", 0),
        ("null", ""),
    ],
)
def test_validate_instance_rejects_a_mismatched_type(declared: str, instance: object) -> None:
    assert _violation_path({"type": declared}, instance) == "$"


@pytest.mark.parametrize("declared", ["integer", "number"])
@pytest.mark.parametrize("instance", [True, False])
def test_validate_instance_does_not_treat_a_boolean_as_a_number(
    declared: str, instance: bool
) -> None:
    assert _violation_path({"type": declared}, instance) == "$"


@pytest.mark.parametrize("instance", [0, 1])
def test_validate_instance_does_not_treat_an_integer_as_a_boolean(instance: int) -> None:
    assert _violation_path({"type": "boolean"}, instance) == "$"


def test_validate_instance_accepts_integers_as_numbers_and_numbers_as_numbers() -> None:
    validate_instance({"type": "number"}, 3)
    validate_instance({"type": "number"}, 3.25)
    validate_instance({"type": "integer"}, 3)


def test_validate_instance_accepts_any_listed_type() -> None:
    schema: dict[str, object] = {"type": ["string", "null"]}

    validate_instance(schema, "a")
    validate_instance(schema, None)
    with pytest.raises(CapabilityInputSchemaError, match="string or null"):
        validate_instance(schema, 3)


# --- validate_instance: enum and const ---------------------------------------------------------


def test_validate_instance_enum_accepts_a_member_and_rejects_the_rest() -> None:
    schema: dict[str, object] = {"enum": ["a", 2, None]}

    for member in ("a", 2, None):
        validate_instance(schema, member)
    assert _violation_path(schema, "b") == "$"


@pytest.mark.parametrize(("option", "instance"), [(1, True), (0, False), (True, 1), (False, 0)])
def test_validate_instance_enum_and_const_keep_booleans_distinct_from_numbers(
    option: object, instance: object
) -> None:
    assert _violation_path({"enum": [option]}, instance) == "$"
    assert _violation_path({"const": option}, instance) == "$"


def test_validate_instance_const_requires_equality() -> None:
    schema: dict[str, object] = {"const": {"a": 1}}

    validate_instance(schema, {"a": 1})
    assert _violation_path(schema, {"a": 2}) == "$"


def test_validate_instance_enum_is_checked_inside_properties() -> None:
    schema: dict[str, object] = {"properties": {"mode": {"enum": ["fast", "slow"]}}}

    assert _violation_path(schema, {"mode": "medium"}) == "$.mode"


# --- validate_instance: objects ----------------------------------------------------------------


def test_validate_instance_reports_a_missing_required_property_by_path() -> None:
    schema: dict[str, object] = {"type": "object", "required": ["name", "id"]}

    assert _violation_path(schema, {"name": "x"}) == "$.id"


def test_validate_instance_checks_required_before_additional_properties() -> None:
    schema: dict[str, object] = {
        "required": ["a"],
        "additionalProperties": False,
        "properties": {"a": {}},
    }

    assert _violation_path(schema, {"z": 1}) == "$.a"


def test_validate_instance_rejects_additional_properties_when_forbidden() -> None:
    schema: dict[str, object] = {"additionalProperties": False, "properties": {"a": {}}}

    assert _violation_path(schema, {"a": 1, "b": 2}) == "$.b"


def test_validate_instance_forbids_every_property_without_properties_declared() -> None:
    assert _violation_path({"additionalProperties": False}, {"a": 1}) == "$.a"
    validate_instance({"additionalProperties": False}, {})


def test_validate_instance_allows_additional_properties_when_true() -> None:
    validate_instance({"additionalProperties": True, "properties": {"a": {}}}, {"a": 1, "b": 2})


def test_validate_instance_validates_declared_properties() -> None:
    schema: dict[str, object] = {"properties": {"a": {"type": "string"}}}

    assert _violation_path(schema, {"a": 1}) == "$.a"


def test_validate_instance_does_not_require_a_declared_property() -> None:
    validate_instance({"properties": {"a": {"type": "string"}}}, {})


def test_validate_instance_reports_the_path_of_a_deeply_nested_violation() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"qty": {"type": "integer", "minimum": 1}},
                },
            }
        },
    }
    instance = {"orders": [{"qty": 1}, {"qty": 2}, {"qty": 0}]}

    assert _violation_path(schema, instance) == "$.orders[2].qty"


# --- validate_instance: arrays -----------------------------------------------------------------


def test_validate_instance_validates_each_array_item() -> None:
    schema: dict[str, object] = {"type": "array", "items": {"type": "integer"}}

    validate_instance(schema, [1, 2, 3])
    assert _violation_path(schema, [1, "2", 3]) == "$[1]"


def test_validate_instance_enforces_min_items() -> None:
    schema: dict[str, object] = {"type": "array", "minItems": 2}

    validate_instance(schema, [1, 2])
    assert _violation_path(schema, [1]) == "$"


def test_validate_instance_enforces_max_items() -> None:
    schema: dict[str, object] = {"type": "array", "maxItems": 2}

    validate_instance(schema, [1, 2])
    assert _violation_path(schema, [1, 2, 3]) == "$"


# --- validate_instance: strings and numbers ----------------------------------------------------


def test_validate_instance_enforces_string_length_bounds() -> None:
    schema: dict[str, object] = {"type": "string", "minLength": 2, "maxLength": 3}

    validate_instance(schema, "ab")
    validate_instance(schema, "abc")
    assert _violation_path(schema, "a") == "$"
    assert _violation_path(schema, "abcd") == "$"


def test_validate_instance_counts_string_length_in_characters() -> None:
    validate_instance({"type": "string", "maxLength": 2}, "\u00e9\u00e9")


def test_validate_instance_enforces_numeric_bounds_inclusively() -> None:
    schema: dict[str, object] = {"type": "number", "minimum": 1, "maximum": 3.5}

    validate_instance(schema, 1)
    validate_instance(schema, 3.5)
    assert _violation_path(schema, 0.99) == "$"
    assert _violation_path(schema, 4) == "$"


def test_validate_instance_names_the_bound_in_the_message() -> None:
    with pytest.raises(CapabilityInputSchemaError, match="below the minimum 5"):
        validate_instance({"minimum": 5}, 1)
    with pytest.raises(CapabilityInputSchemaError, match="above the maximum 5"):
        validate_instance({"maximum": 5}, 9)


# --- validate_instance: instance bounds --------------------------------------------------------


def test_validate_instance_rejects_an_oversized_instance() -> None:
    with pytest.raises(CapabilityInputSchemaError) as caught:
        validate_instance({}, {"blob": "x" * (MAX_INSTANCE_BYTES + 1)})

    assert caught.value.details["limit"] == MAX_INSTANCE_BYTES
    assert "too large" in str(caught.value)


def test_validate_instance_accepts_an_instance_just_under_the_size_limit() -> None:
    validate_instance({}, {"blob": "x" * (MAX_INSTANCE_BYTES - 64)})


def test_validate_instance_rejects_an_instance_nested_too_deeply() -> None:
    schema = _nested_items(MAX_INSTANCE_DEPTH)
    instance = _nested_lists(MAX_INSTANCE_DEPTH)

    with pytest.raises(CapabilityInputSchemaError, match="nested too deeply") as caught:
        validate_instance(schema, instance)

    assert str(caught.value.details["path"]).startswith("$[0]")


def test_validate_instance_rejects_an_instance_that_is_not_json() -> None:
    with pytest.raises(CapabilityInputSchemaError, match="not JSON"):
        validate_instance({}, {"a": object()})
