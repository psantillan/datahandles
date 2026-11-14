"""A live source (the simulated room), recorded windows that never change, and streams of readings."""
import threading
import time

import pytest

from datahandles import Client, HandleError, Runtime, connect
from datahandles.protocols.http import Server


def wait_for(room, seq: int, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            latest = room.invoke("room.latest")
            if latest["seq"] >= seq:
                return latest
        except HandleError:
            pass
        time.sleep(0.01)
    raise TimeoutError(f"no reading {seq}")


@pytest.fixture
def paul(tmp_path):
    return Client(Runtime(tmp_path / "state"), "paul")


def window_graph(handle: str, capability: str, input: dict) -> dict:
    return {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
            "nodes": [{"id": "src", "kind": "resource", "reference": handle},
                      {"id": "op", "kind": "operation", "operation": {"capability": capability, "input": input}}],
            "edges": [{"from": "src", "to": "op"}]}


def test_a_room_makes_readings_and_a_recorded_window_never_changes(paul):
    room = paul.register("room", adapter="room", source={"interval_ms": 10, "seed": 3})
    assert room.describe()["consistency"] == "continuous"
    wait_for(room, 20)
    window = room.invoke("room.record", {"from_seq": 5, "count": 10})
    assert window.info["kind"] == "result" and window.describe()["consistency"] == "snapshot"
    stats = window.invoke("window.stats")
    assert (stats["count"], stats["first_seq"], stats["last_seq"]) == (10, 5, 14)
    assert window.invoke("window.read", {"n": 3})["readings"] == room.invoke("room.read", {"from_seq": 5, "count": 3})["readings"]
    wait_for(room, 40)
    assert window.invoke("window.stats") == stats  # the room went on; the window did not change
    with pytest.raises(HandleError) as e:
        room.invoke("room.read", {"from_seq": 10**6, "count": 1})
    assert e.value.code == "INVALID_INPUT" and "latest_seq" in e.value.details


def test_the_heater_is_an_action_that_changes_the_readings(paul):
    room = paul.register("room", adapter="room", source={"interval_ms": 5, "start": 15.0, "outside": 15.0})
    heater = next(c for c in room.describe()["capabilities"] if c["name"] == "room.heater")
    assert heater["effects"] == {"read": True, "write": True, "external": True} and not heater["execution"]["idempotent"]
    switched = room.invoke("room.heater", {"on": True})["from_seq"]
    wait_for(room, switched + 20)
    after = room.invoke("room.read", {"from_seq": switched + 1, "count": 20})["readings"]
    assert all(r["heater"] for r in after) and after[-1]["temperature"] > after[0]["temperature"] + 2


def test_a_graph_over_a_recorded_window_gives_equal_outputs_on_each_replay(tmp_path, paul):
    room = paul.register("room", adapter="room", source={"interval_ms": 10})
    wait_for(room, 30)
    window = room.invoke("room.record", {"from_seq": 0, "count": 30})
    graph = window_graph(window.id, "window.stats", {})
    first = paul.submit(graph)["results"]["op"]["output"]
    wait_for(room, 50)  # the live source changes
    again = paul.submit(graph)["results"]["op"]
    assert again["output"] == first and again["metadata"]["reused"]
    window.grant("bob", ["*"])
    fresh = Client(paul.runtime, "bob").submit(graph)["results"]["op"]
    assert fresh["output"] == first and not fresh["metadata"].get("reused")  # ran again, same output
    # A record node on the live room is deterministic: a new room version does not run it again.
    recording = window_graph(room.id, "room.record", {"from_seq": 0, "count": 30})
    made = paul.submit(recording)
    wait_for(room, 70)
    assert paul.submit(recording)["results"]["op"]["metadata"]["reused"]
    assert paul.resolve(made["handles"]["op"]).invoke("window.stats") == first


def test_a_subscriber_gets_each_new_reading_in_order(paul):
    room = paul.register("room", adapter="room", source={"interval_ms": 10})
    start = wait_for(room, 2)["seq"]
    events = room.subscribe("room.readings", {"after_seq": start})
    got = [next(events) for _ in range(5)]
    events.close()
    assert [r["seq"] for r in got] == list(range(start + 1, start + 6))
    with pytest.raises(HandleError) as e:
        room.invoke("room.readings")
    assert e.value.code == "INVALID_INPUT"
    with pytest.raises(HandleError) as e:
        next(room.subscribe("room.latest"))
    assert e.value.code == "INVALID_INPUT"


def test_a_subscription_works_over_http_as_server_sent_events(tmp_path):
    runtime = Runtime(tmp_path / "state")
    server = Server(runtime, {"t": "paul"}, creators={"paul"}).serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        paul = connect(f"http://127.0.0.1:{server.server_port}", token="t")
        room = paul.register("room", adapter="room", source={"interval_ms": 10})
        events = room.subscribe("room.readings", {"after_seq": -1})
        got = [next(events)["seq"] for _ in range(3)]
        events.close()
        assert got == [0, 1, 2]
        with pytest.raises(HandleError) as e:
            next(room.subscribe("room.latest"))
        assert e.value.code == "INVALID_INPUT"
    finally:
        server.shutdown()
        server.server_close()


def test_an_event_larger_than_the_size_limit_ends_the_stream(tmp_path):
    from datahandles.core import capability
    from datahandles.runtime.adapter import ResourceAdapter, ResourceRef, Value

    class Loud(ResourceAdapter):
        name = "loud"

        def open(self, source):
            return ResourceRef("loud", {}, "1")

        def describe(self, ref):
            return {"description": "Events that grow."}

        def discover(self, ref):
            return [capability("loud.events", "Each event is larger.", mode="stream", max_output_bytes=100)]

        def observe(self, ref):
            return {"version": "1"}

        def invoke(self, ref, capability, input, *, eager, call):
            raise AssertionError("not called")

        def stream(self, ref, capability, input, call):
            for size in (10, 50, 500):
                yield Value("x" * size, "1")

    paul = Client(Runtime(tmp_path / "state", adapters=(Loud(),)), "paul")
    events = paul.register("loud", adapter="loud", source={}).subscribe("loud.events")
    assert [len(next(events)), len(next(events))] == [10, 50]
    with pytest.raises(HandleError) as e:
        next(events)
    assert e.value.code == "RESULT_TOO_LARGE"
