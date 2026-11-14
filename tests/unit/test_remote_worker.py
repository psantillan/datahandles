"""The remote worker: it does only the tasks that its runtime signed, one time each, and placement follows the handle."""
import json
import threading
import time

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import new_id
from datahandles.core.wire import post
from datahandles.workers.remote import SIGNATURE, Worker, sign

KEY = "the shared key"


@pytest.fixture
def worker(tmp_path):
    server = Worker(tmp_path / "worker", KEY, ["memory"]).serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def send(url, task, key=KEY, body=None):
    body = body or json.dumps(task).encode()
    return post(url, "/v1/task", body, {SIGNATURE: sign(key, json.dumps(task).encode())})[1]


def test_a_worker_refuses_a_task_that_is_changed_expired_or_repeated(worker):
    def task(**fields):
        return {"method": "open", "adapter": "memory", "source": {"value": 1}, "id": new_id("tsk"), "expires": time.time() + 30, **fields}

    good = task()
    assert send(worker, good)["result"]["type"] == "object"
    assert "done before" in send(worker, good)["error"]["message"]
    assert "signature" in send(worker, task(), key="another key")["error"]["message"]
    changed = task()
    assert "signature" in send(worker, changed, body=json.dumps({**changed, "source": {"value": 2}}).encode())["error"]["message"]
    assert "expiry" in send(worker, task(expires=time.time() - 1))["error"]["message"]
    assert send(worker, task(adapter="duckdb"))["error"]["code"] == "INVALID_INPUT"  # not in the allow list of this worker


def test_placement_follows_the_handle(worker, tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    with pytest.raises(HandleError) as e:
        paul.register("far", adapter="memory", source={"value": {}}, worker="w1")
    assert e.value.code == "WORKER_UNAVAILABLE" and not e.value.retryable  # no such worker is registered
    paul.runtime.add_worker("w1", worker, KEY)
    with pytest.raises(HandleError) as e:
        paul.register("far", adapter="filesystem", source=str(tmp_path), worker="w1")
    assert e.value.code == "INVALID_INPUT" and e.value.details == {"adapters": ["memory"]}

    far = paul.register("far", adapter="memory", source={"value": {"a": {"b": 1}}}, worker="w1")
    near = paul.register("near", adapter="memory", source={"value": {"a": {"b": 1}}})
    # A derived handle stays on the worker of its parent, and a job runs there too.
    inner = far.invoke("object.select", {"path": ["a"]})
    assert inner.invoke("object.get") == {"b": 1} == inner.invoke("object.get", background=True).wait(timeout=10)
    assert paul.runtime.registry.get(inner.id).adapter == "memory@w1" and paul.runtime.adapter("memory")._objects.keys() == {paul.runtime.registry.get(near.id).locator["key"]}

    # Two input handles of one operation must be in the same place.
    with pytest.raises(HandleError) as e:
        far.invoke("object.equals", {"other": near})
    assert e.value.code == "INVALID_INPUT" and far.invoke("object.equals", {"other": inner}) == {"equal": False}

    def run(handle, mode):
        return paul.runtime.execute("paul", {"id": new_id("op"), "target": handle.id, "capability": "object.get",
                                             "input": {}, "execution": {"mode": mode}})
    # execution.mode is a limit on the place. It does not move a resource.
    assert run(far, "remote")["output"] == run(near, "local")["output"] == run(far, "auto")["output"]
    for handle, mode, place in ((far, "local", "remote"), (near, "remote", "local")):
        refused = run(handle, mode)
        assert (refused["error"]["code"], refused["error"]["details"]) == ("INVALID_INPUT", {"placement": place})

    # After a restart of the runtime, the registry still knows the worker.
    again = Runtime(tmp_path / "state")
    assert again.registry.worker("w1")["url"] == worker and again.adapter("memory@w1").name == "memory@w1"
