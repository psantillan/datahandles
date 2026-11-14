"""The runtime: resolve a handle, do the checks in order, call the adapter, record the operation.

Check order (ARCHITECTURE.md section 9.1): principal -> handle -> grant -> capability -> input schema -> dispatch -> record.
The caller of `execute` supplies the principal. A transport (HTTP, MCP) must authenticate it first.
"""
import importlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import HandleError, check_input, edit_distance, new_id, topological_order, violations
from .adapter import Derived, ResourceAdapter, Value
from .registry import Record, Registry

ALL = "*"  # a grant on every capability of a handle
# An id that is this many edits from the given text is offered as a correction. One dropped, added,
# or changed character is 1 edit. Two swapped neighbors are 2 edits.
NEAR = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_handle_input(v) -> bool:
    return isinstance(v, dict) and set(v) == {"$handle"}


def _allows(held: set, capability: str) -> bool:
    return ALL in held or capability in held


class Runtime:
    def __init__(self, state_dir=".datahandles", adapters: tuple[ResourceAdapter, ...] = ()):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.state_dir / "registry.sqlite")
        self.registry.drop_ephemeral()
        self._adapters = {a.name: a for a in adapters}

    def adapter(self, name: str) -> ResourceAdapter:
        """Return an adapter. A name that is not loaded yet is a module in datahandles.adapters."""
        if name not in self._adapters:
            if not name.isidentifier():
                raise ValueError(f"bad adapter name: {name!r}")
            self._adapters[name] = importlib.import_module(f"datahandles.adapters.{name}").Adapter(self.state_dir)
        return self._adapters[name]

    # ---- policy -------------------------------------------------------------------------------

    def _held(self, principal: str, rec: Record) -> set:
        """The capabilities that a principal holds on a handle.

        A grant sits on one handle. A derived handle also gives what the principal holds on ALL of
        its parents, so a derivation never gives more authority than its inputs.
        """
        held = self.registry.grants(principal, rec.id)
        if rec.parents:
            inherited = {ALL}
            for parent in rec.parents:
                p = self._held(principal, self.registry.get(parent))
                inherited = p if ALL in inherited else inherited if ALL in p else inherited & p
            held |= inherited
        return held

    def _released(self, rec: Record) -> bool:
        return rec.released_at is not None or any(self._released(self.registry.get(p)) for p in rec.parents)

    def _not_found(self, principal: str, given, message: str) -> HandleError:
        """Make a HANDLE_NOT_FOUND error. Offer the ids that the caller probably means.

        A candidate is an id that the given text contains, or that is NEAR edits from it. Only a
        live handle that the principal holds a capability on is offered, so the error tells a
        principal nothing about a handle that it cannot use.
        """
        near = []
        for handle_id in self.registry.live_ids() if isinstance(given, str) else ():
            if handle_id in given or (abs(len(given) - len(handle_id)) <= NEAR and edit_distance(given, handle_id) <= NEAR):
                rec = self.registry.get(handle_id)
                if self._held(principal, rec) and not self._released(rec):
                    near.append(handle_id)
        return HandleError("HANDLE_NOT_FOUND", message, details={"did_you_mean": near} if near else None)

    def _resolve(self, principal: str, handle_id: str) -> Record:
        rec = self.registry.get(handle_id)
        # A principal with no grant gets the same answer as for an id that does not exist.
        if rec is None or not self._held(principal, rec):
            raise self._not_found(principal, handle_id, f"no handle {handle_id}")
        if self._released(rec):
            raise HandleError("HANDLE_RELEASED", f"handle {handle_id} or one of its parents was released")
        return rec

    def _capability(self, principal: str, rec: Record, name: str) -> dict:
        held = self._held(principal, rec)
        caps = {c["name"]: c for c in self.adapter(rec.adapter).discover(rec.ref)}
        permitted = sorted(c for c in caps if _allows(held, c))
        if name not in caps:
            raise HandleError("CAPABILITY_NOT_FOUND", f"{rec.type} has no capability {name}", details={"permitted": permitted})
        if not _allows(held, name):
            raise HandleError("CAPABILITY_DENIED", f"no grant for {name} on {rec.id}", details={"permitted": permitted})
        return caps[name]

    def _bind(self, principal: str, rec: Record, capability: str, input: dict) -> tuple[dict, list]:
        """Replace each {"$handle": id} input with the reference of that handle. Return (input, parent ids)."""
        bound, parents = dict(input), []
        for field, value in input.items():
            if not _is_handle_input(value):
                continue
            other = self._resolve(principal, value["$handle"])
            if not _allows(self._held(principal, other), capability):
                raise HandleError("CAPABILITY_DENIED", f"no grant for {capability} on {other.id}")
            if other.adapter != rec.adapter:
                raise HandleError("INVALID_INPUT", f"{field}: handle {other.id} belongs to adapter {other.adapter}, not {rec.adapter}")
            bound[field] = other.ref
            parents.append(other.id)
        return bound, parents

    # ---- views --------------------------------------------------------------------------------

    def _view(self, principal: str, rec: Record, *, brief: bool = False) -> dict:
        """The protocol Handle that a principal sees. It lists only the capabilities that the principal holds.

        brief: give each capability by name only (the HandleSummary of an OperationResult).
        """
        held = self._held(principal, rec)
        caps = [c for c in self.adapter(rec.adapter).discover(rec.ref) if _allows(held, c["name"])]
        if brief:
            caps = [c["name"] for c in caps]
        handle = {
            "id": rec.id, "kind": rec.kind, "type": rec.type, "version": rec.version,
            "descriptor": rec.descriptor, "capabilities": caps, "consistency": rec.consistency,
            "lifecycle": {"created_at": rec.created_at, "persistence": rec.persistence},
        }
        if rec.parents:
            handle["parents"] = rec.parents
        return handle

    def find(self, principal: str, name: str) -> dict:
        """Resolve a resource name to the newest live handle that the principal holds a grant on."""
        for handle_id in self.registry.named(name):
            rec = self.registry.get(handle_id)
            if self._held(principal, rec):
                return self._view(principal, rec)
        raise self._not_found(principal, name, f"no handle with the name {name!r}")

    def describe(self, principal: str, handle_id: str) -> dict:
        return self._view(principal, self._resolve(principal, handle_id))

    def observe(self, principal: str, handle_id: str) -> dict:
        rec = self._resolve(principal, handle_id)
        return self.adapter(rec.adapter).observe(rec.ref)

    # ---- lifecycle ----------------------------------------------------------------------------

    def register(self, principal: str, name: str, adapter: str, source: dict) -> dict:
        """Create a resource handle. The principal becomes the owner and holds every capability."""
        a = self.adapter(adapter)
        ref = a.open(source)
        rec = Record(
            id=new_id("hdl"), kind="resource", type=ref.type, adapter=a.name, locator=ref.locator,
            version=ref.version, descriptor={"name": name, **a.describe(ref)}, consistency="latest",
            persistence=a.persistence, created_at=_now(), released_at=None, owner=principal, parents=[], operation=None,
        )
        self.registry.put(rec)
        self.registry.grant(principal, rec.id, [ALL])
        return self._view(principal, rec)

    def release(self, principal: str, handle_id: str) -> None:
        """End a handle. Handles derived from it end too. The external resource stays."""
        rec = self._resolve(principal, handle_id)
        if rec.owner != principal:
            raise HandleError("CAPABILITY_DENIED", "only the owner can release a handle")
        self.adapter(rec.adapter).release(rec.ref, rec.kind)
        self.registry.mark_released(rec.id, _now())

    def grant(self, principal: str, handle_id: str, to: str, capabilities: list[str]) -> None:
        """Give another principal some capabilities on a handle. A principal can give only what it holds."""
        rec = self._resolve(principal, handle_id)
        held = self._held(principal, rec)
        missing = [c for c in capabilities if not _allows(held, c)]
        if missing:
            raise HandleError("CAPABILITY_DENIED", f"cannot grant what you do not hold: {missing}")
        self.registry.grant(to, rec.id, capabilities)

    def revoke(self, principal: str, handle_id: str, from_: str) -> None:
        rec = self._resolve(principal, handle_id)
        if rec.owner != principal:
            raise HandleError("CAPABILITY_DENIED", "only the owner can revoke a grant")
        self.registry.revoke(from_, rec.id)

    # ---- execution ----------------------------------------------------------------------------

    def execute(self, principal: str, operation: dict) -> dict:
        """Run one Operation and return an OperationResult. A protocol error is a failed result, not an exception."""
        started, t0 = _now(), time.perf_counter()
        bad = violations("operations", operation)
        op_id = new_id("op") if bad else operation["id"]
        result = {"operation_id": op_id, "status": "completed", "metadata": {}}
        derived = None
        try:
            if bad:
                raise HandleError("INVALID_INPUT", bad[0], details={"violations": bad})
            rec = self._resolve(principal, operation["target"])
            cap = self._capability(principal, rec, operation["capability"])
            check_input(cap["input_schema"], operation["input"])
            input, more_parents = self._bind(principal, rec, cap["name"], operation["input"])
            eager = operation["execution"].get("strategy") == "eager"
            try:
                out = self.adapter(rec.adapter).invoke(rec.ref, cap["name"], input, eager=eager)
            except HandleError:
                raise
            except Exception as e:  # an adapter fault must not stop the runtime
                raise HandleError("ADAPTER_ERROR", f"{type(e).__name__}: {e}") from e
            if cap["returns"] == "handle":
                assert isinstance(out, Derived), f"{cap['name']} must return Derived"
                derived = self._derive(principal, rec, more_parents, operation, out)
                result["handles"] = [self._view(principal, derived, brief=True)]
                version, size = out.ref.version, 0
            else:
                assert isinstance(out, Value), f"{cap['name']} must return Value"
                size = len(json.dumps(out.output).encode())
                limit = cap["limits"]["max_output_bytes"]
                if size > limit:
                    raise HandleError("RESULT_TOO_LARGE", f"the output is {size} bytes and the limit is {limit}",
                                      details={"bytes": size, "max_output_bytes": limit})
                result["output"], version = out.output, out.version
            result["metadata"]["bytes_transferred"] = size
            if version:
                result["metadata"]["source_version"] = version
        except HandleError as e:
            result["status"], result["error"] = "failed", e.to_dict()
        result["metadata"]["execution_time_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        self.registry.log(
            id=op_id, principal=principal, target=str(operation.get("target")), capability=str(operation.get("capability")),
            input=operation.get("input"), status=result["status"], error_code=result.get("error", {}).get("code"),
            started_at=started, duration_ms=result["metadata"]["execution_time_ms"],
            bytes_transferred=result["metadata"].get("bytes_transferred"),
            source_version=result["metadata"].get("source_version"), result_handle=derived and derived.id,
        )
        return result

    def _derive(self, principal: str, rec: Record, more_parents: list, operation: dict, out: Derived) -> Record:
        parents = [rec.id, *more_parents]
        ephemeral = any(self.registry.get(p).persistence == "ephemeral" for p in parents)
        new = Record(
            id=new_id("hdl"), kind=out.kind, type=out.ref.type, adapter=rec.adapter, locator=out.ref.locator,
            version=out.ref.version if out.kind == "result" else "",
            descriptor=self.adapter(rec.adapter).describe(out.ref), consistency=out.consistency or rec.consistency,
            persistence="ephemeral" if ephemeral else "persistent", created_at=_now(), released_at=None,
            owner=principal, parents=parents,
            operation={k: operation[k] for k in ("id", "capability", "input")},
        )
        self.registry.put(new)
        return new

    # ---- graphs -------------------------------------------------------------------------------

    def graph(self, principal: str, handle_id: str) -> dict:
        """Export the computation graph that made a handle (spec/protocol/graphs.schema.json)."""
        nodes, edges, seen = [], [], set()

        def walk(rec: Record) -> None:
            if rec.id in seen:
                return
            seen.add(rec.id)
            if not rec.parents:
                nodes.append({"id": rec.id, "kind": "resource", "reference": rec.id})
                return
            for parent in rec.parents:
                walk(self.registry.get(parent))
            op = rec.operation
            plain = {k: v for k, v in op["input"].items() if not _is_handle_input(v)}
            nodes.append({"id": op["id"], "kind": "operation", "operation": {"capability": op["capability"], "input": plain}})
            nodes.append({"id": rec.id, "kind": "materialized" if rec.kind == "result" else "derived", "reference": rec.id})
            edges.append({"from": rec.parents[0], "to": op["id"]})
            edges.extend({"from": v["$handle"], "to": op["id"], "input_name": k} for k, v in op["input"].items() if _is_handle_input(v))
            edges.append({"from": op["id"], "to": rec.id})

        top = self._resolve(principal, handle_id)
        walk(top)
        consistency = "snapshot" if top.consistency == "snapshot" else "latest"
        return {"id": new_id("gr"), "nodes": nodes, "edges": edges, "execution": {"strategy": "lazy", "consistency": consistency}}

    def submit(self, principal: str, graph: dict) -> dict:
        """Run a computation graph in dependency order. Each operation goes through `execute`.

        Return {"results": {operation node id: OperationResult}, "handles": {node id: handle id}}.
        The run stops at the first operation that fails.
        """
        bad = violations("graphs", graph)
        if bad:
            raise HandleError("INVALID_INPUT", bad[0], details={"violations": bad})
        nodes = {n["id"]: n for n in graph["nodes"]}
        incoming = {i: [e for e in graph["edges"] if e["to"] == i] for i in nodes}
        bound, results = {}, {}
        for nid in topological_order(graph):
            node, sources = nodes[nid], incoming[nid]
            if node["kind"] == "resource":
                bound[nid] = node["reference"]
            elif node["kind"] == "operation":
                targets = [bound[e["from"]] for e in sources if "input_name" not in e]
                if len(targets) != 1:
                    raise HandleError("INVALID_INPUT", f"operation node {nid} needs exactly one target edge")
                input = dict(node["operation"]["input"])
                input.update({e["input_name"]: {"$handle": bound[e["from"]]} for e in sources if "input_name" in e})
                stored = any(nodes[e["to"]]["kind"] == "materialized" for e in graph["edges"] if e["from"] == nid)
                eager = stored or graph["execution"]["strategy"] == "eager"
                results[nid] = r = self.execute(principal, {
                    "id": new_id("op"), "target": targets[0], "capability": node["operation"]["capability"], "input": input,
                    "execution": {"mode": "auto", "strategy": "eager" if eager else "lazy"}})
                if r["status"] != "completed":
                    break
                if "handles" in r:
                    bound[nid] = r["handles"][0]["id"]
            else:  # derived or materialized: the output of the one operation before it
                if len(sources) != 1 or sources[0]["from"] not in bound:
                    raise HandleError("INVALID_INPUT", f"node {nid} must follow one operation that returns a handle")
                bound[nid] = bound[sources[0]["from"]]
        return {"results": results, "handles": bound}
