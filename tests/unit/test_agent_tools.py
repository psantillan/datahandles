"""The two-tool surface gives a caller that is not Python code the same protocol behavior."""
import json

from datahandles import Client, Runtime
from datahandles.protocols.tools import TOOLS, dispatch


def test_a_tool_caller_can_discover_compose_and_recover(tmp_path):
    owner = Client(Runtime(tmp_path), "owner")
    config = owner.register("config", adapter="memory", source={"value": {"users": [{"name": "ana"}, {"name": "raj"}]}})
    config.grant("agent", ["object.select", "object.get"])
    agent = Client(owner.runtime, "agent")
    assert [t["function"]["name"] for t in TOOLS] == ["handle_describe", "handle_invoke"]

    described = dispatch(agent, "handle_describe", {"handle": config.id})
    assert [c["name"] for c in described["capabilities"]] == ["object.get", "object.select", "log.own"]  # only what the agent holds
    assert "input_schema" not in described["capabilities"][0] and "ana" not in json.dumps(described)  # brief: no schemas
    one = dispatch(agent, "handle_describe", {"handle": config.id, "capability": "object.get"})
    assert one["capability"] == "object.get" and "path" in one["input_schema"]["properties"]
    assert dispatch(agent, "handle_describe", {"handle": config.id, "capability": "object.set"})["error"]["code"] == "CAPABILITY_DENIED"

    users = dispatch(agent, "handle_invoke", {"handle": config.id, "capability": "object.select", "input": {"path": ["users"]}})
    assert set(users) == {"handle"} and users["handle"]["kind"] == "computation"
    assert "capabilities" not in users["handle"]  # a derived handle comes back as a short summary
    got = dispatch(agent, "handle_invoke", {"handle": users["handle"]["id"], "capability": "object.get", "input": {"path": [1, "name"]}})
    assert got == {"output": "raj"}

    # Each failure is a typed value that the caller can act on. None is an exception.
    denied = dispatch(agent, "handle_invoke", {"handle": config.id, "capability": "object.set", "input": {"path": ["x"], "value": 1}})
    assert denied["error"]["code"] == "CAPABILITY_DENIED" and denied["error"]["details"]["permitted"] == ["log.own", "object.get", "object.select"]
    assert dispatch(agent, "handle_invoke", {"handle": config.id, "capability": "object.get", "input": {"nope": 1}})["error"]["code"] == "INVALID_INPUT"
    assert dispatch(agent, "handle_invoke", {"handle": "hdl_" + "0" * 26, "capability": "object.get"})["error"]["code"] == "HANDLE_NOT_FOUND"
    assert dispatch(agent, "handle_invoke", {"handle": 7, "capability": "object.get"})["error"]["code"] == "INVALID_INPUT"
    assert dispatch(agent, "handle_delete", {})["error"]["code"] == "INVALID_INPUT"


def test_the_brief_describe_has_no_schemas_and_one_capability_has_its_schema(tmp_path):
    from datahandles import Client, Runtime
    from datahandles.core import violations
    paul = Client(Runtime(tmp_path / "state"), "paul")
    config = paul.register("config", adapter="memory", source={"value": {"a": 1}})
    brief = config.describe(brief=True)
    assert violations("handles", brief) == [] and all(set(c) == {"name", "description", "returns"} for c in brief["capabilities"])
    one = config.describe(capability="object.get")
    assert [c["name"] for c in one["capabilities"]] == ["object.get"] and "input_schema" in one["capabilities"][0]
