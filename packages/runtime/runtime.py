"""The runtime: resolve a handle, do the checks in order, call the adapter, record the operation.

Check order (ARCHITECTURE.md section 9.1): principal -> handle -> grant -> capability -> input schema -> dispatch -> record.
The caller of `execute` supplies the principal. A transport (HTTP, MCP) must authenticate it first.

Placement: a handle lives where its resource is. The adapter name `duckdb` is the adapter in this
process. The name `duckdb@w1` is the same adapter on the registered remote worker `w1`.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import HandleError, check_input, edit_distance, new_id, topological_order, violations
from .adapter import Call, Derived, Known, Locked, ResourceAdapter, ResourceRef, Value
from .jobs import JobAdapter
from .plugins import adapter_class
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
    def __init__(self, state_dir=".datahandles", adapters: tuple[ResourceAdapter, ...] = (), job_threads: int | None = None):
        """job_threads: how many jobs run at the same time. The default is one for each CPU (os.cpu_count)."""
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.state_dir / "registry.sqlite")
        self.registry.drop_ephemeral()
        self._lock = threading.RLock()  # the adapter table and job submission
        self._adapters = {a.name: Locked(a) for a in adapters}
        self._adapters["job"] = JobAdapter(self.registry, _now, self._cancel_running)
        self._job_threads = job_threads or os.cpu_count() or 1
        self._queue, self._threads = threading.Condition(), []
        self._calls: dict[str, Call] = {}  # job id -> the adapter call of its running attempt
        self._recover()

    def adapter(self, name: str) -> ResourceAdapter:
        """Return an adapter. A name that is not loaded yet is a built-in adapter or an installed one (see plugins), here or on a worker."""
        with self._lock:
            if name not in self._adapters:
                kind, _, worker_id = name.partition("@")
                if not kind.isidentifier():
                    raise HandleError("INVALID_INPUT", f"no adapter {kind!r}")
                if worker_id:
                    worker = self.registry.worker(worker_id)
                    if worker is None:
                        raise HandleError("WORKER_UNAVAILABLE", f"no worker {worker_id!r} is registered")
                    from ..workers.remote import RemoteAdapter  # not at the top: the worker module is also a program
                    self._adapters[name] = RemoteAdapter(kind, worker)
                else:
                    self._adapters[name] = Locked(adapter_class(kind)(self.state_dir))
            return self._adapters[name]

    def add_worker(self, worker_id: str, url: str, key: str) -> dict:
        """Register a remote worker and return what it reports: its adapters and its machine resources.

        This is an operator action. No transport gives it to a principal. The registry file holds the
        key, so the state directory must stay private.
        """
        if not worker_id.isidentifier():
            raise ValueError(f"bad worker id: {worker_id!r}")
        from ..workers.remote import call
        info = call(url, key, {"method": "info"})
        with self._lock:
            self.registry.put_worker(worker_id, url, key, info, _now())
            for name in [n for n in self._adapters if n.endswith("@" + worker_id)]:
                del self._adapters[name]
        return info

    # ---- policy -------------------------------------------------------------------------------

    def _held(self, principal: str, rec: Record) -> set:
        """The capabilities that a principal holds on a handle.

        A grant sits on one handle. A derived handle also gives what the principal holds on ALL of
        its parents, so a derivation never gives more authority than its inputs. A job handle is the
        exception: it belongs to its submitter, and nothing on it comes from the parents.
        """
        held = self.registry.grants(principal, rec.id)
        if rec.parents and rec.kind != "job":
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

    def handles(self, principal: str) -> list[dict]:
        """The live resource handles that the principal holds a capability on, as summaries, oldest first."""
        recs = [self.registry.get(i) for i in self.registry.live_ids("resource")]
        return [self._view(principal, rec, brief=True) for rec in recs if self._held(principal, rec)]

    def describe(self, principal: str, handle_id: str) -> dict:
        return self._view(principal, self._resolve(principal, handle_id))

    def observe(self, principal: str, handle_id: str) -> dict:
        rec = self._resolve(principal, handle_id)
        return self.adapter(rec.adapter).observe(rec.ref)

    # ---- lifecycle ----------------------------------------------------------------------------

    def register(self, principal: str, name: str, adapter: str, source: dict, worker: str | None = None) -> dict:
        """Create a resource handle. The principal becomes the owner and holds every capability.

        worker: the id of the remote worker that can reach the resource. The source is then a location on that worker.
        """
        a = self.adapter(f"{adapter}@{worker}" if worker else adapter)
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
        """Do one Operation and return an OperationResult. A protocol error is a failed result, not an exception.

        With execution.async true, or for a capability with the execution mode `async`, the runtime
        does all checks now and puts the operation in the job queue. The result then has the status
        `running` and one job handle. A second submission of the same operation passes the same checks
        and gives the same job.
        """
        return self._perform(principal, operation, queue=True)

    def _perform(self, principal: str, operation: dict, *, queue: bool, call: Call | None = None) -> dict:
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
            execution = operation["execution"]
            placed = "remote" if "@" in rec.adapter else "local"
            if execution["mode"] not in ("auto", placed):
                raise HandleError("INVALID_INPUT", f"handle {rec.id} is {placed}, so execution.mode {execution['mode']} is not possible",
                                  details={"placement": placed})
            if queue and (execution.get("async") or cap["execution"]["mode"] == "async"):
                derived = self._enqueue(principal, rec, more_parents, operation, cap)
                result["status"], result["handles"] = "running", [self._view(principal, derived, brief=True)]
                size, version = 0, ""
            else:
                out = self._call_adapter(rec, cap, input, execution.get("strategy") == "eager", call or Call())
                if isinstance(out, Known):  # job.result: the handle that the job made
                    made = self._resolve(principal, out.handle_id)
                    result["handles"] = [self._view(principal, made, brief=True)]
                    version, size = made.version, 0
                elif cap["returns"] == "handle":
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

    def _call_adapter(self, rec: Record, cap: dict, input: dict, eager: bool, call: Call):
        """Call the adapter. With `limits.timeout_ms` on the capability, stop waiting at that time.

        At the time limit the runtime cancels the call (see Call) and gives OPERATION_TIMEOUT at once.
        The adapter thread goes on until the adapter honours the cancel. Its output is then discarded.
        """
        adapter = self.adapter(rec.adapter)

        def run():
            try:
                return adapter.invoke(rec.ref, cap["name"], input, eager=eager, call=call)
            except HandleError:
                raise
            except Exception as e:  # an adapter fault must not stop the runtime
                raise HandleError("ADAPTER_ERROR", f"{type(e).__name__}: {e}") from e

        timeout_ms = cap.get("limits", {}).get("timeout_ms")
        if timeout_ms is None:
            return run()
        box, done = {}, threading.Event()

        def work():
            try:
                box["out"] = run()
            except HandleError as e:
                box["error"] = e
            finally:
                done.set()
            if call.cancelled and isinstance(box.get("out"), Derived) and box["out"].kind == "result":
                adapter.release(box["out"].ref, "result")  # nobody will get a handle to this stored output

        threading.Thread(target=work, name="datahandles-call", daemon=True).start()
        if not done.wait(timeout_ms / 1000):
            call.cancel("timeout")
            if not done.is_set():
                raise HandleError("OPERATION_TIMEOUT", f"{cap['name']} did not end in {timeout_ms} ms", details={"timeout_ms": timeout_ms})
        if "error" in box:
            raise box["error"]
        return box["out"]

    def _derive(self, principal: str, rec: Record, more_parents: list, operation: dict, out: Derived, adapter: str | None = None) -> Record:
        parents = [rec.id, *more_parents]
        adapter = adapter or rec.adapter
        ephemeral = any(self.registry.get(p).persistence == "ephemeral" for p in parents)
        new = Record(
            id=new_id("hdl"), kind=out.kind, type=out.ref.type, adapter=adapter, locator=out.ref.locator,
            version=out.ref.version if out.kind == "result" else "",
            descriptor=self.adapter(adapter).describe(out.ref), consistency=out.consistency or rec.consistency,
            persistence="ephemeral" if ephemeral else "persistent", created_at=_now(), released_at=None,
            owner=principal, parents=parents,
            operation={k: operation[k] for k in ("id", "capability", "input")},
        )
        self.registry.put(new)
        return new

    # ---- jobs ---------------------------------------------------------------------------------

    def _duplicate(self, principal: str, operation) -> dict | None:
        """The job that the same principal made earlier from exactly this operation, if there is one."""
        job = self.registry.job(operation["id"]) if isinstance(operation, dict) and isinstance(operation.get("id"), str) else None
        return job if job and job["principal"] == principal and job["operation"] == operation else None

    def _enqueue(self, principal: str, rec: Record, more_parents: list, operation: dict, cap: dict) -> Record:
        """Store a job and make its handle. The submitter holds every capability on the job handle.

        The job handle derives from the handles of the operation. So it ends when one of them is
        released, and it is ephemeral when one of them is ephemeral.
        """
        with self._lock:
            job = self._duplicate(principal, operation)
            if job:
                return self.registry.get(job["handle_id"])
            if self.registry.job(operation["id"]):
                raise HandleError("INVALID_INPUT", f"the operation id {operation['id']} belongs to another job")
            self.registry.add_job(id=operation["id"], principal=principal, operation=operation, capability=cap, submitted_at=_now())
            handle = self._derive(principal, rec, more_parents, operation,
                                  Derived(ResourceRef("job", {"job": operation["id"]}), kind="job"), adapter="job")
            self.registry.grant(principal, handle.id, [ALL])
            self.registry.queue_job(operation["id"], handle.id)
        self._start(1)
        return handle

    def _start(self, jobs: int) -> None:
        """Start the job threads if they are not running, and wake one thread for each new job."""
        with self._queue:
            while len(self._threads) < self._job_threads:
                thread = threading.Thread(target=self._work, name=f"datahandles-jobs-{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                thread.start()
            self._queue.notify(jobs)

    def _next_job(self) -> dict:
        with self._queue:
            while (job := self.registry.start_job(_now())) is None:
                self._queue.wait()
            call = self._calls[job["id"]] = Call()
            if job["cancel"]:
                call.cancel("cancel")
            return job

    def _cancel_running(self, job_id: str) -> None:
        """job.cancel on a running job: cancel its adapter call (see Call)."""
        with self._queue:
            call = self._calls.get(job_id)
        if call:
            call.cancel("cancel")

    def _work(self) -> None:
        """A job thread: take the oldest queued job, run it, repeat. Several threads do this at the same time."""
        while True:
            job = self._next_job()
            try:
                # All checks run again here. A grant that was revoked after the submission stops the job.
                result = self._perform(job["principal"], job["operation"], queue=False, call=self._calls[job["id"]])
            except Exception as e:  # a fault in one job must not stop the job thread
                result = {"operation_id": job["id"], "status": "failed", "metadata": {},
                          "error": HandleError("ADAPTER_ERROR", f"{type(e).__name__}: {e}").to_dict()}
            with self._queue:
                self._calls.pop(job["id"], None)
            state = self.registry.end_job(job["id"], job["attempts"], result["status"], _now(), result)
            if state != result["status"] and "handles" in result:
                # The job was cancelled, or a restart replaced this attempt. The handle that it made must not stay.
                made = self.registry.get(result["handles"][0]["id"])
                self.adapter(made.adapter).release(made.ref, made.kind)
                self.registry.mark_released(made.id, _now())

    def _recover(self) -> None:
        """Deal with the jobs that an earlier runtime process left (ARCHITECTURE.md section 13).

        A queued job runs. A job that was running runs again only if its capability is idempotent.
        If not, the job fails with JOB_INTERRUPTED, because the runtime cannot know if the effect occurred.
        """
        for job in self.registry.jobs("running"):
            if job["capability"]["execution"]["idempotent"] and not job["cancel"]:
                self.registry.requeue_job(job["id"])
            else:
                error = HandleError("JOB_INTERRUPTED", f"the runtime stopped during attempt {job['attempts']} and the capability is not idempotent",
                                    details={"attempts": job["attempts"]})
                self.registry.end_job(job["id"], job["attempts"], "failed", _now(),
                                      {"operation_id": job["id"], "status": "failed", "metadata": {}, "error": error.to_dict()})
        queued = len(self.registry.jobs("queued"))
        if queued:
            self._start(queued)

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
