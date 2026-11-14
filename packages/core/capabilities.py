"""Helper that adapters use to declare a capability."""

# ASSUMED: 64 KiB. No measurement is behind this number. It is the default size limit of one
# value output. An adapter can set a different limit on each capability.
DEFAULT_MAX_OUTPUT_BYTES = 65536

HANDLE_INPUT = {
    "type": "object",
    "required": ["$handle"],
    "additionalProperties": False,
    "properties": {"$handle": {"type": "string"}},
}


def capability(name, description, properties=None, *, required=(), returns="value", write=False,
               external=False, idempotent=True, max_output_bytes=None, max_items=None, output_schema=None) -> dict:
    """Build a capability descriptor (spec/protocol/capabilities.schema.json)."""
    if returns == "value" and max_output_bytes is None:
        max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
    cap = {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": list(required),
            "properties": properties or {},
        },
        "output_schema": output_schema or {},
        "returns": returns,
        "effects": {"read": True, "write": write, "external": external},
        "execution": {"mode": "sync", "idempotent": idempotent},
    }
    limits = {k: v for k, v in (("max_output_bytes", max_output_bytes), ("max_items", max_items)) if v is not None}
    if limits:
        cap["limits"] = limits
    return cap
