"""Computation graph helpers: order the nodes and reject a cycle."""
from .errors import HandleError


def topological_order(graph: dict) -> list[str]:
    """Return the node ids so that each node comes after the nodes it depends on (Kahn's algorithm)."""
    ids = [n["id"] for n in graph["nodes"]]
    waiting = {i: 0 for i in ids}
    out = {i: [] for i in ids}
    for e in graph["edges"]:
        if e["from"] not in waiting or e["to"] not in waiting:
            raise HandleError("INVALID_INPUT", f"edge {e['from']} -> {e['to']} names a node that is not in the graph")
        waiting[e["to"]] += 1
        out[e["from"]].append(e["to"])
    ready = [i for i in ids if waiting[i] == 0]
    order = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for j in out[i]:
            waiting[j] -= 1
            if waiting[j] == 0:
                ready.append(j)
    if len(order) != len(ids):
        raise HandleError("GRAPH_CYCLE", "the graph has a cycle", details={"nodes": sorted(set(ids) - set(order))})
    return order
