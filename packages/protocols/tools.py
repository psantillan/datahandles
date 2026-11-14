"""The agent-facing tool surface: two tools that let a language model use any handle.

The tool definitions are plain JSON Schema function definitions. A transport (an MCP server, a
chat API with tool calls) passes them to a model and passes each tool call to `dispatch`.
The surface does not know any resource type. A model learns what a handle can do from
`handle_describe`, the same way a program does.
"""
from ..core import HandleError
from ..sdk import Client

TOOLS = [
    {"type": "function", "function": {
        "name": "handle_describe",
        "description": "Describe a handle: what the resource is, its schema, and the capabilities you can invoke on it, with the input schema of each.",
        "parameters": {"type": "object", "required": ["handle"], "properties": {
            "handle": {"type": "string", "description": "A handle id, for example hdl_01ABC..."}}},
    }},
    {"type": "function", "function": {
        "name": "handle_invoke",
        "description": "Invoke one capability on a handle. The result is a small output value, or a new handle to use in a later call. The data stays at the resource.",
        "parameters": {"type": "object", "required": ["handle", "capability", "input"], "properties": {
            "handle": {"type": "string", "description": "The id of the handle to operate on."},
            "capability": {"type": "string", "description": "A capability name from handle_describe, for example dataset.filter."},
            "input": {"type": "object", "description": "The input object. It must match the input_schema of the capability. Use {} when the capability needs no input."}}},
    }},
]


def _summary(info: dict) -> dict:
    out = {"id": info["id"], "kind": info["kind"], "type": info["type"]}
    if info["descriptor"].get("schema") is not None:
        out["schema"] = info["descriptor"]["schema"]
    return out


def dispatch(client: Client, name: str, arguments: dict) -> dict:
    """Run one tool call for the principal of `client`. A failure is a value: {"error": {...}}."""
    try:
        if name == "handle_describe":
            info = client.resolve(arguments["handle"]).describe()
            return {**_summary(info), "description": info["descriptor"].get("description", ""), "capabilities": [
                {"name": c["name"], "description": c["description"], "returns": c["returns"], "input_schema": c["input_schema"]}
                for c in info["capabilities"]]}
        if name == "handle_invoke":
            result = client.resolve(arguments["handle"]).call(arguments["capability"], arguments.get("input") or {})
            if result["status"] != "completed":
                return {"error": result["error"]}
            if "handles" in result:
                return {"handle": _summary(result["handles"][0])}
            return {"output": result["output"]}
        return {"error": {"code": "INVALID_INPUT", "message": f"no tool {name!r}", "retryable": False}}
    except HandleError as e:
        return {"error": e.to_dict()}
    except (KeyError, TypeError, AttributeError) as e:  # the model sent arguments of the wrong shape
        return {"error": {"code": "INVALID_INPUT", "message": f"bad tool arguments: {type(e).__name__}: {e}", "retryable": False}}
