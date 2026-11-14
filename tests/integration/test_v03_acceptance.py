"""Acceptance for v0.3, with a scripted client: through the MCP server, record a sensor window,
fork a simulation from it, change one node of a graph, run it again, and see the action wait.

benchmarks/agent_v03.py gives the same task to a language model.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
import anyio  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

spec = importlib.util.spec_from_file_location("v03_fixture", Path(__file__).parents[2] / "benchmarks" / "v03_fixture.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_an_agent_records_a_window_edits_a_graph_and_the_action_waits(tmp_path):
    runtime, server, url, token, handles = fixture.start(tmp_path / "state")
    try:
        command = StdioServerParameters(command=sys.executable, args=["-m", "datahandles.protocols.mcp", "--url", url],
                                        env={**os.environ, "DATAHANDLES_TOKEN": token})

        async def session():
            async with Client(command) as client:
                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    return result.is_error, result.structured_content

                async def invoke(handle, capability, input):
                    failed, out = await call("handle_invoke", {"handle": handle, "capability": capability, "input": input})
                    assert not failed, out
                    return out

                found = {h["name"]: h["id"] for h in (await call("handle_find", {}))[1]["handles"]}
                seq = (await invoke(found["room"], "room.latest", {}))["output"]["seq"]
                window = (await invoke(found["room"], "room.record", {"from_seq": seq - 19, "count": 20}))["handle"]["id"]
                mean = (await invoke(window, "window.stats", {}))["output"]["mean"]
                edited = (await invoke(found["plan"], "graph.set", {"node": "start", "input": {"temperature": mean}}))["handle"]["id"]
                before = len(runtime.registry.history())
                run = (await invoke(edited, "graph.run", {}))["output"]
                ran = fixture.ran_nodes(runtime, before)
                approve = await call("handle_invoke", {"handle": edited, "capability": "graph.approve", "input": {"node": "switch"}})
                return mean, run, ran, approve

        mean, run, ran, approve = anyio.run(session)
        results = run["results"]
        assert results["baseline"]["metadata"]["reused"]  # the node that does not depend on `start`
        assert not any(results[n]["metadata"].get("reused") for n in ("start", "heat", "predict_on", "cool", "predict_off"))
        assert results["switch"]["status"] == "waiting"
        assert results["predict_on"]["output"]["temperature"] == fixture.expected(mean)
        assert results["predict_off"]["output"]["temperature"] < results["predict_on"]["output"]["temperature"]  # two forks
        assert sorted(ran) == ["graph.run", "sim.set", "sim.state", "sim.state", "sim.step", "sim.step"]  # no heater, no baseline
        assert approve[0] and approve[1]["error"]["code"] == "CAPABILITY_DENIED"  # the agent cannot approve the action
    finally:
        server.shutdown()
        server.server_close()
