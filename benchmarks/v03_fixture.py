"""The fixture of the v0.3 acceptance task: a live room, a simulation of it, and a graph over both.

The runtime runs in this process behind the HTTP transport, so the room (an ephemeral, live
resource) lives as long as the task. The MCP server connects to it with a token.

The graph "plan":
    twin -> baseline (sim.state)                       does not depend on the start node
    twin -> start (sim.set) -> heat (sim.step, heater on)  -> predict_on  (sim.state)
                            -> cool (sim.step, heater off) -> predict_off (sim.state)
    room -> switch (room.heater on)                     an action: it waits for an approval
"""
import threading
import time

from datahandles import Client, Runtime
from datahandles.protocols.http import Server

STEPS = 60
TASK = ("Record the last 20 readings of the room as a window and get their mean temperature. "
        "In the graph named plan, set the input of the node `start` to that mean temperature (graph.set), "
        f"then run the new graph. Tell me the predicted temperature after {STEPS} steps with the heater on, "
        "and whether the heater switch ran or waits for an approval.")


def plan(twin: str, room: str) -> dict:
    def op(node_id, capability, input):
        return {"id": node_id, "kind": "operation", "operation": {"capability": capability, "input": input}}

    def out(node_id):
        return {"id": node_id, "kind": "derived"}

    nodes = [{"id": "twin", "kind": "resource", "reference": twin}, {"id": "room", "kind": "resource", "reference": room},
             op("baseline", "sim.state", {}), op("start", "sim.set", {"temperature": 18.0}), out("start_state"),
             op("heat", "sim.step", {"steps": STEPS, "heater": True}), out("heated"), op("predict_on", "sim.state", {}),
             op("cool", "sim.step", {"steps": STEPS, "heater": False}), out("cooled"), op("predict_off", "sim.state", {}),
             op("switch", "room.heater", {"on": True})]
    edges = [("twin", "baseline"), ("twin", "start"), ("start", "start_state"), ("start_state", "heat"), ("heat", "heated"),
             ("heated", "predict_on"), ("start_state", "cool"), ("cool", "cooled"), ("cooled", "predict_off"), ("room", "switch")]
    return {"id": "plan", "nodes": nodes, "edges": [{"from": a, "to": b} for a, b in edges],
            "execution": {"strategy": "lazy", "consistency": "latest"}}


def expected(mean: float, outside: float = 15.0) -> float:
    """The temperature after STEPS steps with the heater on, from the model of the sim adapter."""
    from datahandles.adapters.room import HEAT, LOSS
    t = mean
    for _ in range(STEPS):
        t += LOSS * (outside - t) + HEAT
    return round(t, 3)


def start(state_dir):
    """Start the runtime, the room, the twin, and the plan. Run the plan one time for the agent, so a reuse is possible.

    Return (runtime, server, url, token, handles). The agent may run and edit the plan, but not approve it.
    """
    runtime = Runtime(state_dir)
    owner, agent = Client(runtime, "owner"), Client(runtime, "agent")
    room = owner.register("room", adapter="room", source={"interval_ms": 50, "seed": 7, "start": 19.0})
    twin = owner.register("twin", adapter="sim", source={"temperature": 18.0, "outside": 15.0})
    graph = owner.register("plan", adapter="graph", source={"graph": plan(twin.id, room.id)})
    # The agent can read and record the room, and read the windows it records (a derived handle gives
    # what its parent gives, by capability name). It cannot switch the heater: that needs an approval.
    room.grant("agent", ["room.latest", "room.read", "room.record", "window.stats", "window.read"])
    twin.grant("agent", ["*"])
    graph.grant("agent", ["graph.run", "graph.set"])
    first = agent.resolve(graph.id).invoke("graph.run")
    assert first["results"]["switch"]["status"] == "waiting"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and int(agent.resolve(room.id).observe()["version"]) < 21:
        time.sleep(0.05)
    server = Server(runtime, {"agent-token": "agent"}).serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return runtime, server, f"http://127.0.0.1:{server.server_port}", "agent-token", {"room": room, "twin": twin, "plan": graph}


def ran_nodes(runtime, since: int) -> list[str]:
    """The capabilities that ran (operation log rows) after row number `since`."""
    return [op["capability"] for op in runtime.registry.history()[since:]]
