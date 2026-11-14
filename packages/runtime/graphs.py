"""A computation graph as a resource. A graph handle can run, and it can derive an edited graph.

The definition of a graph handle never changes. `graph.set` derives a new graph handle with new
input for one node, so each version of a workflow has its own handle and lineage.
The runtime does the capabilities of this adapter itself (Runtime._graph), because a graph runs
operations for a principal, and an adapter never sees a principal.
"""
import json
from pathlib import Path

from ..core import HandleError, capability, digest, topological_order, violations
from .adapter import ResourceAdapter, ResourceRef

_NODE = {"type": "string", "description": "The id of an operation node of this graph."}
_CAPS = [
    capability("graph.run", "Run the graph. A node that did not change gives its stored result and does not run again. "
               "The output has the result of each operation node and the handle of each node."),
    capability("graph.set", "Derive a graph with a new input for one operation node. The other nodes stay.",
               {"node": _NODE, "input": {"type": "object"}}, required=["node", "input"], returns="handle"),
    capability("graph.approve", "Allow one run of a node that changes the world. Without an approval such a node waits.",
               {"node": _NODE}, required=["node"], write=True, idempotent=False),
]


def is_action(cap: dict) -> bool:
    """A capability that changes the world: it writes, it reaches outside, and a second run is not safe."""
    return cap["effects"]["write"] and cap["effects"]["external"] and not cap["execution"]["idempotent"]


def check(graph: dict) -> dict:
    bad = violations("graphs", graph)
    if bad:
        raise HandleError("INVALID_INPUT", bad[0], details={"violations": bad})
    topological_order(graph)  # GRAPH_CYCLE
    return graph


def summary(graph: dict) -> dict:
    """The nodes and edges of a graph in a short form for a caller."""
    nodes = []
    for n in graph["nodes"]:
        item = {"id": n["id"], "kind": n["kind"]}
        if "reference" in n:
            item["reference"] = n["reference"]
        if "operation" in n:
            item.update(capability=n["operation"]["capability"], input=n["operation"]["input"])
        nodes.append(item)
    return {"nodes": nodes, "edges": graph["edges"]}


class GraphAdapter(ResourceAdapter):
    name = "graph"

    def open(self, source: dict) -> ResourceRef:
        graph = source.get("graph")
        if graph is None:
            if not source.get("path"):
                raise HandleError("INVALID_INPUT", "a graph source needs `graph` (the graph) or `path` (a JSON file)")
            graph = json.loads(Path(source["path"]).read_text())
        return ResourceRef("graph", {"graph": check(graph)}, digest(graph)[:16])

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "A computation graph: operation nodes on handles, with a handle at each step.",
                "schema": summary(ref.locator["graph"])}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        return {"version": ref.version}

    def invoke(self, ref, capability, input, *, eager, call):
        raise HandleError("ADAPTER_ERROR", "the runtime does the capabilities of a graph")
