"""The MCP adapter: an MCP client finds, describes, and uses a handle through three fixed tools."""
import sys

import pytest

pytest.importorskip("mcp")
import anyio
from mcp import Client, StdioServerParameters


def test_an_mcp_client_uses_a_resource_through_the_three_tools(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("the launch is on friday\n" * 3)
    command = StdioServerParameters(command=sys.executable, args=[
        "-m", "datahandles.protocols.mcp", "--state", str(tmp_path / "state"), "--register", f"docs=filesystem:{docs}"])

    async def session():
        async with Client(command) as client:
            tools = (await client.list_tools()).tools

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                return result.is_error, result.structured_content

            found = await call("handle_find", {})
            handle = found[1]["handles"][0]["id"]
            return {
                "tools": [t.name for t in tools],
                "schema": next(t.input_schema for t in tools if t.name == "handle_invoke"),
                "found": found,
                "described": await call("handle_describe", {"handle": handle}),
                "read": await call("handle_invoke", {"handle": handle, "capability": "filesystem.read", "input": {"path": "notes.txt", "length": 23}}),
                "outside": await call("handle_invoke", {"handle": handle, "capability": "filesystem.read", "input": {"path": "../state/registry.sqlite"}}),
                "wrong id": await call("handle_invoke", {"handle": handle[:-1], "capability": "filesystem.list", "input": {}}),
            }

    got = anyio.run(session)
    assert got["tools"] == ["handle_find", "handle_describe", "handle_invoke"]
    assert got["schema"]["required"] == ["handle", "capability", "input"]
    assert got["found"][0] is False and [(h["name"], h["type"]) for h in got["found"][1]["handles"]] == [("docs", "filesystem")]
    assert str(docs) not in str(got["found"]) + str(got["described"])  # a handle shows no storage location
    assert "filesystem.read" in [c["name"] for c in got["described"][1]["capabilities"]]
    assert got["read"] == (False, {"output": {**got["read"][1]["output"], "text": "the launch is on friday"}})
    # A protocol error is a tool result with the error flag and the typed code. The model can act on it.
    assert (got["outside"][0], got["outside"][1]["error"]["code"]) == (True, "CAPABILITY_DENIED")
    assert got["wrong id"][0] is True and got["wrong id"][1]["error"]["details"]["did_you_mean"] == [got["found"][1]["handles"][0]["id"]]
