"""Asynchronous jobs: a job is a handle, its state is durable, and a restart deals with it by rule."""
import threading

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import capability, new_id, violations
from datahandles.runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value


class Gate(ResourceAdapter):
    """A resource whose operations wait until the test opens the gate."""
    name = "gate"

    def __init__(self, *, idempotent=True, is_open=False):
        self.opened, self.entered, self.calls = threading.Event(), threading.Event(), 0
        if is_open:
            self.opened.set()
        self._caps = [capability("gate.pass", "Wait for the gate, then return a number.", idempotent=idempotent),
                      capability("gate.fork", "Wait for the gate, then derive a handle.", returns="handle")]

    def open(self, source):
        return ResourceRef("gate", {}, "1")

    def describe(self, ref):
        return {"description": "A gate."}

    def discover(self, ref):
        return self._caps

    def observe(self, ref):
        return {"version": "1"}

    def invoke(self, ref, capability, input, *, eager, call):
        self.calls += 1
        self.entered.set()
        assert self.opened.wait(10), "the test did not open the gate"
        return Value({"passed": True}, "1") if capability == "gate.pass" else Derived(ResourceRef("gate", {"fork": 1}, "1"), kind="result")


def start(tmp_path, **gate):
    """One job thread, so a second job stays queued while the first one runs."""
    adapter = Gate(**gate)
    paul = Client(Runtime(tmp_path / "state", adapters=(adapter,), job_threads=1), "paul")
    return adapter, paul, paul.register("gate", adapter="gate", source={})


def state(job):
    return job.invoke("job.status")["state"]


def test_a_background_operation_gives_a_job_handle_and_then_the_result(tmp_path):
    adapter, paul, gate = start(tmp_path)
    accepted = gate.call("gate.pass", background=True)
    assert violations("results", accepted) == []
    assert accepted["status"] == "running" and "output" not in accepted
    job = paul.resolve(accepted["handles"][0]["id"])
    assert violations("handles", job.info) == []
    assert job.info["kind"] == "job" and sorted(job.discover()) == ["job.cancel", "job.result", "job.status"]
    assert adapter.entered.wait(10)
    assert state(job) == "running"
    with pytest.raises(HandleError) as e:
        job.invoke("job.result")
    assert e.value.code == "JOB_NOT_FINISHED" and e.value.retryable
    adapter.opened.set()
    assert job.wait(timeout=10) == {"passed": True} == gate.invoke("gate.pass")
    assert state(job) == "completed"
    # A job of a capability that returns a handle gives that handle, and the caller holds it.
    made = gate.invoke("gate.fork", background=True).wait(timeout=10)
    assert made.info["kind"] == "result" and made.describe()["parents"] == [gate.id]
    # A job handle belongs to its submitter. A second principal gets nothing on it from a grant on the target.
    gate.grant("bob", ["*"])
    with pytest.raises(HandleError) as e:
        Client(paul.runtime, "bob").resolve(job.id)
    assert e.value.code == "HANDLE_NOT_FOUND"


def test_cancel_ends_a_queued_job_and_discards_the_output_of_a_running_job(tmp_path):
    adapter, paul, gate = start(tmp_path)
    running = gate.invoke("gate.fork", background=True)
    assert adapter.entered.wait(10)
    queued = gate.invoke("gate.pass", background=True)
    assert state(queued) == "queued"
    assert queued.invoke("job.cancel")["state"] == "cancelled"
    assert running.invoke("job.cancel") == {**running.invoke("job.status"), "cancel_requested": True}
    adapter.opened.set()
    for job in (running, queued):
        with pytest.raises(HandleError) as e:
            job.wait(timeout=10)
        assert e.value.code == "JOB_CANCELLED"
    assert adapter.calls == 1  # the queued job never ran
    made = [row["result_handle"] for row in paul.runtime.registry.history() if row["capability"] == "gate.fork" and row["result_handle"]]
    assert [paul.runtime.registry.get(h).released_at is not None for h in made if h != running.id] == [True]


def test_the_same_submission_makes_one_job(tmp_path):
    adapter, paul, gate = start(tmp_path, is_open=True)
    operation = {"id": new_id("op"), "target": gate.id, "capability": "gate.pass", "input": {},
                 "execution": {"mode": "auto", "async": True}}
    first, again = paul.runtime.execute("paul", operation), paul.runtime.execute("paul", operation)
    assert first["handles"][0]["id"] == again["handles"][0]["id"]
    paul.resolve(first["handles"][0]["id"]).wait(timeout=10)
    assert adapter.calls == 1
    # The same id with other content, or from another principal, is not the same submission.
    gate.grant("bob", ["*"])
    for other in (paul.runtime.execute("paul", {**operation, "capability": "gate.fork"}), paul.runtime.execute("bob", operation)):
        assert other["status"] == "failed" and other["error"]["code"] == "INVALID_INPUT" and "handles" not in other


def test_a_grant_that_is_revoked_stops_a_queued_job(tmp_path):
    adapter, paul, gate = start(tmp_path)
    gate.grant("bob", ["gate.pass"])
    bob = Client(paul.runtime, "bob").resolve(gate.id)
    first = gate.invoke("gate.pass", background=True)
    assert adapter.entered.wait(10)
    queued = bob.invoke("gate.pass", background=True)
    paul.runtime.revoke("paul", gate.id, "bob")
    adapter.opened.set()
    first.wait(timeout=10)
    with pytest.raises(HandleError) as e:
        queued.wait(timeout=10)
    assert e.value.code == "HANDLE_NOT_FOUND" and adapter.calls == 1


@pytest.mark.parametrize("idempotent", [True, False])
def test_an_interrupted_job_runs_again_only_if_the_capability_is_idempotent(tmp_path, idempotent):
    adapter, paul, gate = start(tmp_path, idempotent=idempotent)
    job_id = gate.invoke("gate.pass", background=True).id
    assert adapter.entered.wait(10)
    # A new runtime on the same state directory is what a restart after a crash gives.
    after = Gate(idempotent=idempotent)
    job = Client(Runtime(tmp_path / "state", adapters=(after,)), "paul").resolve(job_id)
    if idempotent:
        assert after.entered.wait(10)  # attempt 2 runs
    before = job.invoke("job.status")
    assert (before["state"], before["attempts"]) == (("running", 2) if idempotent else ("failed", 1))
    # Attempt 1 of the stopped runtime ends late. It must not change the job.
    adapter.opened.set()
    paul.runtime._threads[0].join(0.5)  # give the late attempt time to end
    assert job.invoke("job.status") == before
    after.opened.set()
    if idempotent:
        assert job.wait(timeout=10) == {"passed": True} and after.calls == 1
    else:
        with pytest.raises(HandleError) as e:
            job.wait(timeout=10)
        assert e.value.code == "JOB_INTERRUPTED" and after.calls == 0
