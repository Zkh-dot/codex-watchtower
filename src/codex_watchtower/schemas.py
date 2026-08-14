"""Loading and validation helpers for the committed JSON Schema contracts.

Schemas are the single committed source of truth under ``schemas/`` at the
repository root; this module never copies or restates them.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_FILES: dict[str, str] = {
    "observation": "observation.schema.json",
    "assessment": "assessment.schema.json",
    "assessment_wire": "assessment.wire.schema.json",
    "reconciled_assessment": "reconciled_assessment.schema.json",
}


def _find_schemas_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        probe = candidate / "schemas"
        if (probe / "observation.schema.json").is_file():
            return probe
    raise FileNotFoundError(
        "Could not locate the repository 'schemas/' directory above "
        f"{here}. Set WATCHTOWER_SCHEMAS_DIR to override."
    )


def schemas_dir() -> Path:
    import os

    override = os.environ.get("WATCHTOWER_SCHEMAS_DIR")
    if override:
        return Path(override)
    return _find_schemas_dir()


@cache
def load_raw(name: str) -> dict[str, Any]:
    path = schemas_dir() / SCHEMA_FILES[name]
    data: dict[str, Any] = json.loads(path.read_text())
    return data


@cache
def registry() -> Registry[Any]:
    resources = [Resource.from_contents(load_raw(name)) for name in SCHEMA_FILES]
    entries: list[tuple[str, Resource[Any]]] = []
    for resource in resources:
        resource_id = resource.id()
        if resource_id is not None:
            entries.append((resource_id, resource))
    return Registry().with_resources(entries)


@cache
def validator_for(name: str) -> Draft202012Validator:
    schema = load_raw(name)
    return Draft202012Validator(schema, registry=registry(), format_checker=FormatChecker())


def validate(name: str, instance: dict[str, Any]) -> None:
    validator_for(name).validate(instance)


def is_valid(name: str, instance: dict[str, Any]) -> bool:
    return bool(validator_for(name).is_valid(instance))
