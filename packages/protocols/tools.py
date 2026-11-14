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
        "description": "Describe a handle: what the resource is, its schema, and the capabilities you can invoke on it. "
                       "Give `capability` to get the input schema of that one capability before you invoke it.",
        "parameters": {"type": "object", "required": ["handle"], "properties": {
            "handle": {"type": "string", "description": "A handle id, for example hdl_01ABC..."},
            "capability": {"type": "string", "description": "Optional. One capability name: the answer is its input schema."}}},
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

# A caller that starts with no handle id needs one more tool. A caller that gets its ids in the prompt does not.
FIND_TOOL = {"type": "function", "function": {
    "name": "handle_find",
    "description": "List the resource handles you can use. Give a name to get one handle. Start here when you have no handle id.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "The name of one resource, for example events. Leave it out to list all."}}},
}}


def _summary(info: dict) -> dict:
    out = {"id": info["id"], "kind": info["kind"], "type": info["type"]}
    if info["descriptor"].get("schema") is not None:
        out["schema"] = info["descriptor"]["schema"]
    return out


def dispatch(client: Client, name: str, arguments: dict) -> dict:
    """Run one tool call for the principal of `client`. A failure is a value: {"error": {...}}."""
    try:
        if name == "handle_describe":
            handle = client.resolve(arguments["handle"])
            if arguments.get("capability"):
                c = handle.describe(capability=arguments["capability"])["capabilities"][0]
                return {"capability": c["name"], "description": c["description"], "returns": c["returns"], "input_schema": c["input_schema"]}
            info = handle.describe(brief=True)
            return {**_summary(info), "description": info["descriptor"].get("description", ""), "capabilities": [
                {"name": c["name"], "description": c["description"], "returns": c["returns"]} for c in info["capabilities"]]}
        if name == "handle_invoke":
            result = client.resolve(arguments["handle"]).call(arguments["capability"], arguments.get("input") or {})
            if result["status"] == "failed":
                return {"error": result["error"]}
            if "handles" in result:
                return {"handle": _summary(result["handles"][0])}
            return {"output": result["output"]}
        if name == "handle_find":
            found = [client.resolve(arguments["name"])] if arguments.get("name") else client.handles()
            return {"handles": [{"name": h.info["descriptor"]["name"], **_summary(h.info)} for h in found]}
        return {"error": {"code": "INVALID_INPUT", "message": f"no tool {name!r}", "retryable": False}}
    except HandleError as e:
        return {"error": e.to_dict()}
    except (KeyError, TypeError, AttributeError) as e:  # the model sent arguments of the wrong shape
        return {"error": {"code": "INVALID_INPUT", "message": f"bad tool arguments: {type(e).__name__}: {e}", "retryable": False}}
