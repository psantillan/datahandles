"""Load the protocol schemas from spec/protocol and validate protocol objects against them.

The schemas are the definition of the protocol. This module holds no second copy of them.
"""
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .errors import HandleError

SPEC_DIR = Path(__file__).resolve().parents[2] / "spec" / "protocol"
_SCHEMAS = {p.name.split(".")[0]: json.loads(p.read_text()) for p in sorted(SPEC_DIR.glob("*.schema.json"))}
_REGISTRY = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in _SCHEMAS.values())
_VALIDATORS = {
    k: Draft202012Validator(s, registry=_REGISTRY, format_checker=FormatChecker()) for k, s in _SCHEMAS.items()
}


def violations(kind: str, obj) -> list[str]:
    """Return the schema violations of a protocol object. `kind` is a schema name, for example "handles"."""
    return [f"{'/'.join(map(str, e.path)) or '.'}: {e.message}" for e in _VALIDATORS[kind].iter_errors(obj)]


def check_input(schema: dict, value: dict) -> None:
    """Validate an operation input against the input_schema of its capability."""
    errors = [f"{'/'.join(map(str, e.path)) or '.'}: {e.message}" for e in Draft202012Validator(schema).iter_errors(value)]
    if errors:
        raise HandleError("INVALID_INPUT", errors[0], details={"violations": errors})
