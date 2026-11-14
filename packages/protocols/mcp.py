"""The MCP adapter: the agent tool surface as a Model Context Protocol server on stdio.

An MCP client (a chat application, an agent harness) gets three tools: `handle_find`,
`handle_describe`, and `handle_invoke`. The tools do not change when a resource is added. The
model learns what a handle can do from `handle_describe`. No resource data goes into a tool
definition, and each tool result is a bounded value or a handle.

The server acts for one principal. It needs the `mcp` package (pip install "datahandles[mcp]").

A runtime in this process:
  python -m datahandles.protocols.mcp --state DIR --principal agent --register events=duckdb:/data/events.parquet
A runtime behind the HTTP transport (the bearer token is in the environment variable DATAHANDLES_TOKEN):
  python -m datahandles.protocols.mcp --url http://127.0.0.1:8470
"""
import argparse
import json
import os

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from ..core import HandleError
from ..sdk import Client, connect
from .tools import FIND_TOOL, TOOLS, dispatch

INSTRUCTIONS = ("Data stays at its resource. Call handle_find to get the handles you can use, handle_describe to see what "
                "a handle can do, and handle_invoke to run one capability. A result is a small value or a new handle.")


def server(client: Client) -> Server:
    """Make an MCP server that does each tool call for the principal of `client`."""
    tools = [types.Tool(name=f["name"], description=f["description"], input_schema=f["parameters"])
             for f in (t["function"] for t in (FIND_TOOL, *TOOLS))]

    async def list_tools(context, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(context, params) -> types.CallToolResult:
        # The runtime blocks while an operation runs. A worker thread keeps the MCP event loop free.
        out = await anyio.to_thread.run_sync(dispatch, client, params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(out))],
                                    structured_content=out, is_error="error" in out)

    return Server("datahandles", instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


def main() -> None:
    p = argparse.ArgumentParser(description="Serve DataHandles to an MCP client on stdio.")
    p.add_argument("--state", help="the state directory of a runtime in this process")
    p.add_argument("--url", help="the URL of a runtime behind the HTTP transport; the token is in DATAHANDLES_TOKEN")
    p.add_argument("--principal", default="agent", help="the principal of each tool call (with --state)")
    p.add_argument("--register", action="append", default=[], metavar="NAME=ADAPTER:PATH",
                   help="create a resource handle at the start if the name has none; repeat for more resources")
    a = p.parse_args()
    if bool(a.state) == bool(a.url):
        p.error("give --state or --url, not both")
    client = connect(a.url, token=os.environ["DATAHANDLES_TOKEN"]) if a.url else connect(a.state, a.principal)
    for spec in a.register:
        name, _, rest = spec.partition("=")
        adapter, _, path = rest.partition(":")
        try:
            client.resolve(name)
        except HandleError:
            client.register(name, adapter=adapter, source=path)

    async def run() -> None:
        mcp_server = server(client)
        async with stdio_server() as (read, write):
            await mcp_server.run(read, write, mcp_server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
