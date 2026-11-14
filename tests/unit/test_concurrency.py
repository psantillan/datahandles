"""A lock for each resource, several job threads, cancel of a running call, and limits.timeout_ms."""
import threading
import time

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import capability
from datahandles.runtime.adapter import Locked, ResourceAdapter, ResourceRef, Value
from datahandles.workers.remote import Worker


class Valve(ResourceAdapter):
    """Each resource is a valve. `valve.wait` waits until the test opens that valve, or until the call is cancelled."""
    name = "valve"

    def __init__(self, state_dir=None, *, timeout_ms=None):
        self.opened, self.entered, self.returned = {}, {}, {}
        self._caps = [capability("valve.wait", "Wait for the valve to open.", idempotent=False),
                      capability("valve.peek", "Return at once."),
                      capability("valve.slow", "Wait like valve.wait, with a time limit.")]
        self._caps[2]["limits"]["timeout_ms"] = timeout_ms or 200

    def lock_key(self, ref):
        return ref.locator["name"]

    def open(self, source):
        name = source["name"]
        for table in (self.opened, self.entered, self.returned):
            table[name] = threading.Event()
        return ResourceRef("valve", {"name": name}, "1")

    def describe(self, ref):
        return {"description": "A valve."}

    def discover(self, ref):
        return self._caps

    def observe(self, ref):
        return {"version": "1"}

    def invoke(self, ref, capability, input, *, eager, call):
        name = ref.locator["name"]
        if capability == "valve.peek":
            return Value({"valve": name}, "1")
        call.on_cancel(self.opened[name].set)  # the cancel hook: stop waiting
        self.entered[name].set()
        try:
            assert self.opened[name].wait(10), "the test did not open the valve"
            call.check()
            return Value({"valve": name}, "1")
        finally:
            self.returned[name].set()


def start(tmp_path, adapter=None, **runtime):
    adapter = adapter or Valve()
    paul = Client(Runtime(tmp_path / "state", adapters=(adapter,), **runtime), "paul")
    return adapter, paul, paul.register("a", adapter="valve", source={"name": "a"}), paul.register("b", adapter="valve", source={"name": "b"})


def in_thread(fn):
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("value", fn()), daemon=True)
    thread.start()
    return thread, out


def test_a_long_call_on_one_resource_does_not_delay_a_call_on_a_second_resource(tmp_path):
    valve, paul, a, b = start(tmp_path)
    thread, _ = in_thread(lambda: a.invoke("valve.wait"))
    assert valve.entered["a"].wait(10)
    t0 = time.perf_counter()
    assert b.invoke("valve.peek") == {"valve": "b"}
    assert time.perf_counter() - t0 < 1 and thread.is_alive()
    # Calls on the same resource still take turns.
    second, out = in_thread(lambda: a.invoke("valve.peek"))
    second.join(0.3)
    assert second.is_alive() and "value" not in out
    valve.opened["a"].set()
    second.join(10)
    assert out["value"] == {"valve": "a"}


def test_an_adapter_with_no_lock_key_keeps_one_lock_for_all_its_resources():
    class OneLock(Valve):
        lock_key = ResourceAdapter.lock_key
    adapter = Locked(OneLock())
    a, b = adapter.open({"name": "a"}), adapter.open({"name": "b"})
    from datahandles.runtime.adapter import Call
    thread, _ = in_thread(lambda: adapter.invoke(a, "valve.wait", {}, eager=False, call=Call()))
    assert adapter._adapter.entered["a"].wait(10)
    second, out = in_thread(lambda: adapter.invoke(b, "valve.peek", {}, eager=False, call=Call()))
    second.join(0.3)
    assert "value" not in out
    adapter._adapter.opened["a"].set()
    second.join(10)
    assert out["value"].output == {"valve": "b"}


def test_two_long_jobs_run_at_the_same_time(tmp_path):
    valve, paul, a, b = start(tmp_path, job_threads=2)
    ja, jb = a.invoke("valve.wait", background=True), b.invoke("valve.wait", background=True)
    assert valve.entered["a"].wait(10) and valve.entered["b"].wait(10)
    assert ja.invoke("job.status")["state"] == jb.invoke("job.status")["state"] == "running"
    valve.opened["a"].set()
    valve.opened["b"].set()
    assert ja.wait(timeout=10) == {"valve": "a"} and jb.wait(timeout=10) == {"valve": "b"}


def test_cancel_stops_the_adapter_call_of_a_running_job(tmp_path):
    valve, paul, a, b = start(tmp_path)
    job = a.invoke("valve.wait", background=True)
    assert valve.entered["a"].wait(10)
    job.invoke("job.cancel")
    assert valve.returned["a"].wait(5), "the cancel did not reach the adapter call"
    deadline = time.time() + 5
    while job.invoke("job.status")["state"] != "cancelled" and time.time() < deadline:
        time.sleep(0.02)
    assert job.invoke("job.status")["state"] == "cancelled"
    with pytest.raises(HandleError) as e:
        job.invoke("job.result")
    assert e.value.code == "JOB_CANCELLED"
    assert a.invoke("valve.peek") == {"valve": "a"}  # the lock of the resource is free


def test_a_call_that_passes_its_time_limit_ends_with_a_typed_error_and_frees_its_lock(tmp_path):
    valve, paul, a, b = start(tmp_path)
    t0 = time.perf_counter()
    result = a.call("valve.slow")
    assert result["status"] == "failed" and result["error"]["code"] == "OPERATION_TIMEOUT"
    assert result["error"]["details"] == {"timeout_ms": 200}
    assert time.perf_counter() - t0 < 2
    assert valve.returned["a"].wait(5)
    assert a.invoke("valve.peek") == {"valve": "a"}
    # The same limit applies inside a job.
    valve.opened["a"].clear()  # the cancel hook opened the valve; close it again
    job = a.invoke("valve.slow", background=True)
    with pytest.raises(HandleError) as e:
        job.wait(timeout=10)
    assert e.value.code == "OPERATION_TIMEOUT"


def test_cancel_and_time_limit_reach_an_adapter_call_on_a_remote_worker(tmp_path):
    worker = Worker(tmp_path / "worker", "key", ["memory"])
    remote = Valve()
    worker._adapters["valve"] = Locked(remote)  # a test adapter; a real one loads by name
    server = worker.serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        runtime = Runtime(tmp_path / "state")
        runtime.add_worker("w1", f"http://127.0.0.1:{server.server_port}", "key")
        paul = Client(runtime, "paul")
        a = paul.register("a", adapter="valve", source={"name": "a"}, worker="w1")
        job = a.invoke("valve.wait", background=True)
        assert remote.entered["a"].wait(10)
        job.invoke("job.cancel")
        assert remote.returned["a"].wait(5), "the cancel did not reach the worker"
        remote.opened["a"].clear()  # the cancel hook opened the valve; close it again
        remote.returned["a"].clear()
        result = a.call("valve.slow")
        assert remote.returned["a"].wait(5), "the time limit did not reach the worker"
        assert result["error"]["code"] == "OPERATION_TIMEOUT"
        assert a.invoke("valve.peek") == {"valve": "a"}
    finally:
        server.shutdown()
        server.server_close()


def test_a_hook_that_comes_after_the_cancel_runs_at_once():
    from datahandles.runtime.adapter import Call
    call, ran = Call(), []
    call.cancel("timeout")
    call.on_cancel(lambda: ran.append(1))
    assert ran == [1] and call.cancelled
    with pytest.raises(HandleError) as e:
        call.check()
    assert e.value.code == "OPERATION_TIMEOUT"
