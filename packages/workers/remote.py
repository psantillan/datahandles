"""The remote HTTP worker (ARCHITECTURE.md sections 4.3 and 4.4).

A worker is a process next to some resources. It hosts adapters and does adapter calls for one
runtime. The computation moves to the data: a derived handle stays on the worker of its parent.

The runtime does every check (principal, grant, capability, input schema) before it sends a task.
The worker sees no principal and holds no grant. Each task is one adapter call on one resource. It
carries an HMAC-SHA256 signature over its full body, an expiry time, and an id that the worker
accepts one time. So a task that someone records cannot run later, cannot run twice, and cannot be
changed to a different resource or capability. HTTP gives no secrecy: put the worker on a private
network or behind TLS.

Run a worker:  python -m datahandles.workers.remote --state DIR --key-file FILE --adapters duckdb,filesystem --port 8471
"""
import argparse
import hashlib
import hmac
import http.client
import importlib
import json
import os
import threading
import time
from pathlib import Path

from ..core import HandleError, new_id
from ..core.wire import post, serve
from ..runtime.adapter import Call, Derived, Locked, ResourceAdapter, ResourceRef, Value

# ASSUMED: 30 s. A task is valid for this time after the runtime signs it. The time must cover the
# clock difference between the two machines and the time to send the request. No measurement is behind it.
TASK_TTL_S = 30
SIGNATURE = "X-DataHandles-Signature"


def sign(key: str, body: bytes) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def call(url: str, key: str, task: dict, task_id: str | None = None):
    """Sign one task, send it to a worker, and return the answer. A protocol error from the worker is raised again here."""
    body = json.dumps({**task, "id": task_id or new_id("tsk"), "expires": time.time() + TASK_TTL_S}).encode()
    try:
        _, reply = post(url, "/v1/task", body, {SIGNATURE: sign(key, body)})
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise HandleError("WORKER_UNAVAILABLE", f"no answer from the worker at {url}: {type(e).__name__}: {e}", retryable=True) from e
    if "error" in reply:
        e = reply["error"]
        raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
    return reply["result"]


def _plain(ref: ResourceRef) -> dict:
    return {"type": ref.type, "locator": ref.locator, "version": ref.version}


class RemoteAdapter(ResourceAdapter):
    """The runtime side: an adapter object that sends each call to one worker."""

    def __init__(self, kind: str, worker: dict):
        hosted = worker["info"]["adapters"]
        if kind not in hosted:
            raise HandleError("INVALID_INPUT", f"the worker {worker['id']} does not host the adapter {kind}", details={"adapters": sorted(hosted)})
        self.name, self.persistence = f"{kind}@{worker['id']}", hosted[kind]["persistence"]
        self._kind, self._url, self._key = kind, worker["url"], worker["key"]

    def _task(self, method: str, task_id: str | None = None, **fields):
        return call(self._url, self._key, {"method": method, "adapter": self._kind, **fields}, task_id)

    def open(self, source: dict) -> ResourceRef:
        return ResourceRef(**self._task("open", source=source))

    def describe(self, ref: ResourceRef) -> dict:
        return self._task("describe", ref=_plain(ref))

    def discover(self, ref: ResourceRef) -> list[dict]:
        return self._task("discover", ref=_plain(ref))

    def observe(self, ref: ResourceRef) -> dict:
        return self._task("observe", ref=_plain(ref))

    def release(self, ref: ResourceRef, kind: str) -> None:
        self._task("release", ref=_plain(ref), kind=kind)

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call: Call) -> Value | Derived:
        # A second input handle goes in `refs`, apart from the caller's values. So no value from a caller can pass as a reference.
        refs = {k: _plain(v) for k, v in input.items() if isinstance(v, ResourceRef)}
        task_id = new_id("tsk")
        # A cancel here becomes a cancel task to the worker, which cancels the adapter call there.
        call.on_cancel(lambda: self._task("cancel", task=task_id, reason=call.reason))
        out = self._task("invoke", task_id, ref=_plain(ref), capability=capability, eager=eager, refs=refs,
                         input={k: v for k, v in input.items() if k not in refs})
        if "derived" in out:
            return Derived(ResourceRef(**out["derived"]), out["kind"], out["consistency"])
        return Value(out["output"], out["version"])


class Worker:
    """The worker side: check each task, then call the adapter."""

    def __init__(self, state_dir, key: str, adapters, worker_id: str = "worker"):
        self.state_dir, self.key, self.id = Path(state_dir), key, worker_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name in adapters:
            if not name.isidentifier():
                raise ValueError(f"bad adapter name: {name!r}")
        self._adapters = {name: Locked(importlib.import_module(f"datahandles.adapters.{name}").Adapter(self.state_dir)) for name in adapters}
        self._seen, self._lock = {}, threading.Lock()  # task id -> expiry time
        self._calls: dict[str, Call] = {}  # task id -> the call of a running invoke task

    def info(self) -> dict:
        return {"id": self.id, "adapters": {name: {"persistence": a.persistence} for name, a in self._adapters.items()},
                "resources": {"cpu": os.cpu_count(), "memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")}}

    def _admit(self, body: bytes, signature: str) -> dict:
        if not hmac.compare_digest(sign(self.key, body), signature or ""):
            raise HandleError("WORKER_UNAVAILABLE", "the worker refused the task: no valid signature")
        task, now = json.loads(body), time.time()
        if task["expires"] < now:
            raise HandleError("WORKER_UNAVAILABLE", "the worker refused the task: it is past its expiry time")
        with self._lock:
            self._seen = {i: t for i, t in self._seen.items() if t >= now}
            if task["id"] in self._seen:
                raise HandleError("WORKER_UNAVAILABLE", "the worker refused the task: it was done before")
            self._seen[task["id"]] = task["expires"]
        return task

    def _do(self, task: dict):
        method = task["method"]
        if method == "info":
            return self.info()
        if method == "cancel":
            with self._lock:
                running = self._calls.get(task["task"])
            if running:
                running.cancel(task["reason"])
            return {"cancelled": running is not None}
        if task["adapter"] not in self._adapters:
            raise HandleError("INVALID_INPUT", f"this worker does not host the adapter {task['adapter']}")
        adapter = self._adapters[task["adapter"]]
        if method == "open":
            return _plain(adapter.open(task["source"]))
        ref = ResourceRef(**task["ref"])
        if method in ("describe", "discover", "observe"):
            return getattr(adapter, method)(ref)
        if method == "release":
            return adapter.release(ref, task["kind"])
        if method == "invoke":
            input = {**task["input"], **{k: ResourceRef(**v) for k, v in task["refs"].items()}}
            with self._lock:
                running = self._calls[task["id"]] = Call()
            try:
                out = adapter.invoke(ref, task["capability"], input, eager=task["eager"], call=running)
            finally:
                with self._lock:
                    self._calls.pop(task["id"], None)
            if isinstance(out, Derived):
                return {"derived": _plain(out.ref), "kind": out.kind, "consistency": out.consistency}
            return {"output": out.output, "version": out.version}
        raise HandleError("INVALID_INPUT", f"no task method {method}")

    def answer(self, path: str, headers, body: bytes) -> tuple[int, dict]:
        try:
            if path != "/v1/task":
                raise HandleError("INVALID_INPUT", f"no path {path}")
            return 200, {"result": self._do(self._admit(body, headers.get(SIGNATURE)))}
        except HandleError as e:
            return 200, {"error": e.to_dict()}
        except Exception as e:  # an adapter fault must not stop the worker
            return 200, {"error": HandleError("ADAPTER_ERROR", f"{type(e).__name__}: {e}").to_dict()}

    def serve(self, host: str = "127.0.0.1", port: int = 0):
        return serve(self.answer, host, port)


def main() -> None:
    p = argparse.ArgumentParser(description="Run a DataHandles remote worker.")
    p.add_argument("--state", required=True, help="directory for what the adapters store")
    p.add_argument("--key-file", required=True, help="file with the secret that this worker shares with the runtime")
    p.add_argument("--adapters", required=True, help="adapter names with commas between them, for example duckdb,filesystem")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--id", default="worker")
    a = p.parse_args()
    server = Worker(a.state, Path(a.key_file).read_text().strip(), a.adapters.split(","), a.id).serve(a.host, a.port)
    print(f"http://{a.host}:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
