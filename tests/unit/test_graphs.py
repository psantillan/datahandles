"""Replayable graphs: each node has an identity, and an unchanged node gives its stored result again."""
import copy

from datahandles import Client, Runtime
from datahandles.core import violations

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
