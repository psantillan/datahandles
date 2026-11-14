"""Replayable graphs: each node has an identity, and an unchanged node gives its stored result again."""
import copy
import threading
import time
from collections import defaultdict

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import capability, violations
from datahandles.runtime.adapter import ResourceAdapter, ResourceRef, Value

VALUE = {"a": {"b": {"c": {"d": 1}, "x": {"d": 2}}}}


def chain(root: str, steps: list) -> dict:
    """A graph: the root handle, then one operation for each step, each on the output of the one before."""
    nodes, edges, last = [{"id": "root", "kind": "resource", "reference": root}], [], "root"
    for i, (capability, input) in enumerate(steps):
        nodes.append({"id": f"op{i}", "kind": "operation", "operation": {"capability": capability, "input": input}})
        edges.append({"from": last, "to": f"op{i}"})
        last = f"op{i}"
        if capability != "object.get":
            nodes.append({"id": f"h{i}", "kind": "materialized" if capability == "object.snapshot" else "derived"})
            edges.append({"from": f"op{i}", "to": f"h{i}"})
            last = f"h{i}"
    return {"id": "g", "nodes": nodes, "edges": edges, "execution": {"strategy": "lazy", "consistency": "latest"}}


STEPS = [("object.select", {"path": ["a"]}), ("object.snapshot", {}), ("object.select", {"path": ["b"]}),
         ("object.select", {"path": ["c"]}), ("object.get", {"path": ["d"]})]


def ran(runtime) -> int:
    return len(runtime.registry.history())


def test_a_changed_node_runs_with_the_nodes_after_it_and_the_others_are_reused(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    root = paul.register("config", adapter="memory", source={"value": VALUE})
    graph = chain(root.id, STEPS)
    first = paul.submit(graph)
    assert first["results"]["op4"]["output"] == 1 and not any(r["metadata"].get("reused") for r in first["results"].values())
    # The same graph again: nothing runs.
    before = ran(paul.runtime)
    again = paul.submit(graph)
    assert ran(paul.runtime) == before and all(r["metadata"]["reused"] for r in again["results"].values())
    assert [violations("results", r) for r in again["results"].values()] == [[]] * 5
    assert again["handles"] == first["handles"]
    # Change node op3: op0 to op2 are reused, op3 and op4 run.
    edited = copy.deepcopy(graph)
    next(n for n in edited["nodes"] if n["id"] == "op3")["operation"]["input"]["path"] = ["x"]
    before = ran(paul.runtime)
    changed = paul.submit(edited)
    assert ran(paul.runtime) - before == 2
    assert [changed["results"][f"op{i}"]["metadata"].get("reused", False) for i in range(5)] == [True, True, True, False, False]
    # The output equals a full run on a new runtime.
    fresh = Client(Runtime(tmp_path / "fresh"), "paul")
    steps = [(n["operation"]["capability"], n["operation"]["input"]) for n in edited["nodes"] if n["kind"] == "operation"]
    full = fresh.submit(chain(fresh.register("config", adapter="memory", source={"value": VALUE}).id, steps))
    assert changed["results"]["op4"]["output"] == full["results"]["op4"]["output"] == 2


def test_a_new_version_of_the_source_runs_the_graph_again(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    root = paul.register("config", adapter="memory", source={"value": VALUE})
    graph = chain(root.id, STEPS)
    paul.submit(graph)
    root.invoke("object.set", {"path": ["a", "b", "c", "d"], "value": 5})
    assert VALUE["a"]["b"]["c"]["d"] == 1  # a write changes the stored copy, not the value of the caller
    run = paul.submit(graph)
    assert run["results"]["op4"]["output"] == 5 and not run["results"]["op0"]["metadata"].get("reused")


def test_reuse_belongs_to_one_principal(tmp_path):
    runtime = Runtime(tmp_path / "state")
    paul = Client(runtime, "paul")
    root = paul.register("config", adapter="memory", source={"value": VALUE})
    root.grant("bob", ["*"])
    graph = chain(root.id, STEPS)
    paul.submit(graph)
    run = Client(runtime, "bob").submit(graph)
    assert not any(r["metadata"].get("reused") for r in run["results"].values())


class Slow(ResourceAdapter):
    """`slow.wait` waits until the test opens the gate of its resource, or until the call is cancelled."""
    name = "slow"

    def __init__(self, state_dir=None):
        self.opened, self.entered, self.returned = (defaultdict(threading.Event) for _ in range(3))

    def lock_key(self, ref):
        return ref.locator["name"]

    def open(self, source):
        return ResourceRef("slow", {"name": source["name"]}, "1")

    def describe(self, ref):
        return {"description": "A slow resource."}

    def discover(self, ref):
        return [capability("slow.wait", "Wait for the gate.")]

    def observe(self, ref):
        return {"version": "1"}

    def invoke(self, ref, capability, input, *, eager, call):
        name = ref.locator["name"]
        call.on_cancel(self.opened[name].set)
        self.entered[name].set()
        try:
            assert self.opened[name].wait(10), "the test did not open the gate"
            call.check()
            return Value({"waited": name}, "1")
        finally:
            self.returned[name].set()


def two_branches(tree: str, slow: str) -> dict:
    """A fast node on a directory tree (it survives a restart), then a long node on a slow resource."""
    return {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
            "nodes": [{"id": "tree", "kind": "resource", "reference": tree},
                      {"id": "fast", "kind": "operation", "operation": {"capability": "filesystem.list", "input": {}}},
                      {"id": "slow", "kind": "resource", "reference": slow},
                      {"id": "long", "kind": "operation", "operation": {"capability": "slow.wait", "input": {}}}],
            "edges": [{"from": "tree", "to": "fast"}, {"from": "slow", "to": "long"}]}


def lists(runtime) -> int:
    return sum(1 for op in runtime.registry.history() if op["capability"] == "filesystem.list")


def test_a_graph_is_a_handle_and_an_edit_derives_a_new_graph(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    root = paul.register("config", adapter="memory", source={"value": VALUE})
    g = paul.register("flow", adapter="graph", source={"graph": chain(root.id, STEPS)})
    assert g.info["type"] == "graph" and [n["id"] for n in g.describe()["descriptor"]["schema"]["nodes"]][:2] == ["root", "op0"]
    assert g.invoke("graph.run")["results"]["op4"]["output"] == 1
    edited = g.invoke("graph.set", {"node": "op3", "input": {"path": ["x"]}})
    assert edited.describe()["parents"] == [g.id] and edited.info["kind"] == "result"
    out = edited.invoke("graph.run")
    assert out["results"]["op4"]["output"] == 2 and out["results"]["op2"]["metadata"]["reused"]
    assert g.invoke("graph.run")["results"]["op4"]["output"] == 1  # the first graph did not change


def test_a_graph_with_a_long_node_runs_as_a_job_that_a_caller_can_cancel(tmp_path):
    slow = Slow()
    paul = Client(Runtime(tmp_path / "state", adapters=(slow,)), "paul")
    tree = paul.register("tree", adapter="filesystem", source=str(tmp_path))
    gate = paul.register("gate", adapter="slow", source={"name": "a"})
    g = paul.register("flow", adapter="graph", source={"graph": two_branches(tree.id, gate.id)})
    t0 = time.perf_counter()
    job = g.invoke("graph.run", background=True)
    assert time.perf_counter() - t0 < 1 and slow.entered["a"].wait(10)
    assert job.invoke("job.status")["state"] == "running"
    job.invoke("job.cancel")
    assert slow.returned["a"].wait(5), "the cancel did not reach the node"
    with pytest.raises(HandleError) as e:
        job.wait(timeout=10)
    assert e.value.code == "JOB_CANCELLED"


def test_after_a_restart_a_graph_job_goes_on_and_a_finished_node_does_not_run_again(tmp_path):
    slow = Slow()
    paul = Client(Runtime(tmp_path / "state", adapters=(slow,)), "paul")
    tree = paul.register("tree", adapter="filesystem", source=str(tmp_path))
    gate = paul.register("gate", adapter="slow", source={"name": "a"})
    g = paul.register("flow", adapter="graph", source={"graph": two_branches(tree.id, gate.id)})
    job_id = g.invoke("graph.run", background=True).id
    assert slow.entered["a"].wait(10)
    assert lists(paul.runtime) == 1  # the fast node ran before the long node
    # A new runtime on the same state directory is what a restart after a crash gives.
    after = Slow()
    runtime = Runtime(tmp_path / "state", adapters=(after,))
    assert after.entered["a"].wait(10), "the graph job did not run again"
    after.opened["a"].set()
    out = Client(runtime, "paul").resolve(job_id).wait(timeout=10)
    assert out["results"]["fast"]["metadata"]["reused"] and out["results"]["long"]["output"] == {"waited": "a"}
    assert lists(runtime) == 1  # the fast node did not run again
    slow.opened["a"].set()  # let the attempt of the stopped runtime end


class Lamp(ResourceAdapter):
    """A lamp in the world. `lamp.switch` changes it: it writes, it reaches outside, and it is not idempotent."""
    name = "lamp"

    def __init__(self, state_dir=None):
        self.switches, self.on = 0, False

    def open(self, source):
        return ResourceRef("lamp", {}, "0")

    def describe(self, ref):
        return {"description": "A lamp."}

    def discover(self, ref):
        return [capability("lamp.switch", "Switch the lamp.", {"on": {"type": "boolean"}}, required=["on"],
                           write=True, external=True, idempotent=False),
                capability("lamp.state", "Return the state of the lamp.")]

    def observe(self, ref):
        return {"version": str(self.switches)}

    def invoke(self, ref, capability, input, *, eager, call):
        if capability == "lamp.switch":
            self.switches, self.on = self.switches + 1, input["on"]
        return Value({"on": self.on, "switches": self.switches}, str(self.switches))


def lamp_graph(lamp: str) -> dict:
    """Switch the lamp on, then read its state."""
    return {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
            "nodes": [{"id": "lamp", "kind": "resource", "reference": lamp},
                      {"id": "switch", "kind": "operation", "operation": {"capability": "lamp.switch", "input": {"on": True}}},
                      {"id": "read", "kind": "operation", "operation": {"capability": "lamp.state", "input": {}}}],
            "edges": [{"from": "lamp", "to": "switch"}, {"from": "lamp", "to": "read"}]}


def test_an_action_waits_for_an_approval_and_one_approval_allows_one_run(tmp_path):
    lamp = Lamp()
    runtime = Runtime(tmp_path / "state", adapters=(lamp,))
    paul = Client(runtime, "paul")
    g = paul.register("flow", adapter="graph", source={"graph": lamp_graph(paul.register("lamp", adapter="lamp", source={}).id)})
    first = g.invoke("graph.run")["results"]
    assert first["switch"]["status"] == "waiting" and "read" not in first and lamp.switches == 0
    assert violations("results", first["switch"]) == []
    # Approval needs its own grant: a principal that can run the graph cannot approve.
    g.grant("bob", ["graph.run"])
    with pytest.raises(HandleError) as e:
        Client(runtime, "bob").resolve(g.id).invoke("graph.approve", {"node": "switch"})
    assert e.value.code == "CAPABILITY_DENIED"
    g.invoke("graph.approve", {"node": "switch"})
    ran = g.invoke("graph.run")["results"]
    assert ran["switch"]["output"] == {"on": True, "switches": 1} and ran["read"]["output"]["on"] and lamp.switches == 1
    # A finished action never runs again by itself, also when its target has a new version.
    again = g.invoke("graph.run")["results"]
    assert again["switch"]["metadata"]["reused"] and lamp.switches == 1
    # A new approval is a deliberate second run.
    g.invoke("graph.approve", {"node": "switch"})
    g.invoke("graph.run")
    assert lamp.switches == 2


def test_an_edited_action_is_a_new_action_and_needs_its_own_approval(tmp_path):
    lamp = Lamp()
    paul = Client(Runtime(tmp_path / "state", adapters=(lamp,)), "paul")
    g = paul.register("flow", adapter="graph", source={"graph": lamp_graph(paul.register("lamp", adapter="lamp", source={}).id)})
    g.invoke("graph.approve", {"node": "switch"})
    g.invoke("graph.run")
    off = g.invoke("graph.set", {"node": "switch", "input": {"on": False}})
    assert off.invoke("graph.run")["results"]["switch"]["status"] == "waiting" and lamp.switches == 1
    with pytest.raises(HandleError) as e:
        g.invoke("graph.approve", {"node": "nothing"})
    assert e.value.code == "INVALID_INPUT"
