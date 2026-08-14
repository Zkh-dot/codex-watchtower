from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from codex_watchtower.schemas import load_raw, validate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

SCHEMA_NAMES = ("observation", "assessment", "assessment_wire", "reconciled_assessment")

FORBIDDEN_WIRE_KEYWORDS = {
    "const",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "oneOf",
}


def _load_fixture(*parts: str) -> dict[str, Any]:
    path = FIXTURES.joinpath(*parts)
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def test_all_schemas_are_valid_json_schema() -> None:
    for name in SCHEMA_NAMES:
        Draft202012Validator.check_schema(load_raw(name))


def test_healthy_observation_validates() -> None:
    validate("observation", _load_fixture("observations", "healthy.json"))


def test_healthy_assessment_validates() -> None:
    validate("assessment", _load_fixture("assessments", "healthy.json"))


def test_healthy_reconciled_validates() -> None:
    validate("reconciled_assessment", _load_fixture("reconciled", "healthy.json"))


def test_invalid_assessment_out_of_range_confidence_rejected() -> None:
    instance = _load_fixture("assessments", "healthy.json")
    instance["confidence_percent"] = 150
    with pytest.raises(ValidationError):
        validate("assessment", instance)


def test_invalid_assessment_missing_required_field_rejected() -> None:
    instance = _load_fixture("assessments", "healthy.json")
    del instance["status"]
    with pytest.raises(ValidationError):
        validate("assessment", instance)


def test_invalid_assessment_unknown_status_rejected() -> None:
    instance = _load_fixture("assessments", "healthy.json")
    instance["status"] = "not_a_real_status"
    with pytest.raises(ValidationError):
        validate("assessment", instance)


def test_invalid_assessment_extra_property_rejected() -> None:
    instance = _load_fixture("assessments", "healthy.json")
    instance["unexpected_field"] = "nope"
    with pytest.raises(ValidationError):
        validate("assessment", instance)


# --- wire schema stays a conservative, structurally aligned projection ---


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_wire_schema_omits_strict_mode_risky_keywords() -> None:
    wire = load_raw("assessment_wire")
    for node in _walk(wire):
        present = FORBIDDEN_WIRE_KEYWORDS & node.keys()
        assert not present, f"forbidden keyword(s) {present} found in wire schema node: {node}"


def test_wire_schema_has_no_external_refs() -> None:
    wire = load_raw("assessment_wire")
    for node in _walk(wire):
        ref = node.get("$ref")
        if ref is not None:
            assert ref.startswith("#"), f"external $ref found in wire schema: {ref}"


def test_wire_schema_objects_are_strict() -> None:
    wire = load_raw("assessment_wire")
    for node in _walk(wire):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, node
            assert set(node.get("required", [])) == set(node["properties"].keys()), node


def _type_set(prop: dict[str, Any]) -> set[str]:
    t = prop.get("type")
    if t is None:
        return set()
    if isinstance(t, str):
        return {t}
    return set(t)


def _enum_values(prop: dict[str, Any]) -> set[Any] | None:
    if "enum" in prop:
        return set(prop["enum"])
    if "const" in prop:
        return {prop["const"]}
    return None


def _compare_object(
    auth: dict[str, Any],
    wire: dict[str, Any],
    auth_defs: dict[str, Any],
    wire_defs: dict[str, Any],
    path: str,
) -> None:
    auth_props: dict[str, Any] = auth.get("properties", {})
    wire_props: dict[str, Any] = wire.get("properties", {})
    assert set(auth_props) == set(wire_props), f"{path}: property sets differ"
    assert wire.get("additionalProperties") is False, f"{path}: wire must be strict"
    assert set(wire.get("required", [])) == set(wire_props), f"{path}: wire must require all"

    for name, auth_prop in auth_props.items():
        wire_prop = wire_props[name]
        auth_nullable = "null" in _type_set(auth_prop)
        wire_nullable = "null" in _type_set(wire_prop)
        assert auth_nullable == wire_nullable, f"{path}.{name}: nullability mismatch"

        auth_enum = _enum_values(auth_prop)
        if auth_enum is not None:
            wire_enum = _enum_values(wire_prop)
            assert wire_enum == auth_enum, f"{path}.{name}: enum membership mismatch"

        if "$ref" in auth_prop:
            ref_name = auth_prop["$ref"].rsplit("/", 1)[-1]
            wire_ref_name = wire_prop["$ref"].rsplit("/", 1)[-1]
            _compare_object(
                auth_defs[ref_name],
                wire_defs[wire_ref_name],
                auth_defs,
                wire_defs,
                f"{path}.{name}",
            )
        elif auth_prop.get("type") == "array":
            auth_items = auth_prop.get("items", {})
            wire_items = wire_prop.get("items", {})
            if "$ref" in auth_items:
                ref_name = auth_items["$ref"].rsplit("/", 1)[-1]
                wire_ref_name = wire_items["$ref"].rsplit("/", 1)[-1]
                _compare_object(
                    auth_defs[ref_name],
                    wire_defs[wire_ref_name],
                    auth_defs,
                    wire_defs,
                    f"{path}.{name}[]",
                )
            elif auth_items.get("type") == "object":
                _compare_object(auth_items, wire_items, auth_defs, wire_defs, f"{path}.{name}[]")


def test_wire_schema_matches_authoritative_shape() -> None:
    auth = load_raw("assessment")
    wire = load_raw("assessment_wire")
    _compare_object(auth, wire, auth.get("$defs", {}), wire.get("$defs", {}), "assessment")


# --- every authoritative-valid payload is also wire-valid (superset property) ---


def _valid_assessment_variants() -> list[dict[str, Any]]:
    base = _load_fixture("assessments", "healthy.json")
    variants = [copy.deepcopy(base)]

    with_concern = copy.deepcopy(base)
    with_concern["status"] = "stalled"
    with_concern["needs_attention"] = True
    with_concern["recommended_human_action"] = "Check the last three commands for a loop."
    with_concern["concerns"] = [
        {
            "severity": "warning",
            "kind": "loop",
            "explanation": "Same command repeated three times with no changed outcome.",
            "evidence_refs": ["evt:1"],
        }
    ]
    variants.append(with_concern)

    terra_escalated = copy.deepcopy(base)
    terra_escalated["assessed_by"] = "terra"
    terra_escalated["escalation_reason"] = "Luna reported possibly_aligned goal alignment."
    terra_escalated["goal_alignment"] = "possibly_aligned"
    terra_escalated["status"] = "investigating"
    variants.append(terra_escalated)

    unknown_cursor = copy.deepcopy(base)
    unknown_cursor["event_cursor"] = None
    unknown_cursor["status"] = "idle"
    variants.append(unknown_cursor)

    rules_only = copy.deepcopy(base)
    rules_only["assessed_by"] = "rules"
    rules_only["confidence_percent"] = 0
    variants.append(rules_only)

    return variants


@pytest.mark.parametrize("instance", _valid_assessment_variants())
def test_authoritative_valid_payloads_are_also_wire_valid(instance: dict[str, Any]) -> None:
    validate("assessment", instance)
    validate("assessment_wire", instance)
