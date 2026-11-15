"""Acceptance tests. One test for each row of the table in ARCHITECTURE.md section 16.

Every handle and every result that these tests touch must also pass the protocol schemas.
"""
import json
import os
import threading

import duckdb
import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import DEFAULT_MAX_OUTPUT_BYTES, capability, new_id, violations
from datahandles.runtime.adapter import ResourceAdapter, ResourceRef, Value
from datahandles.workers.remote import Worker

EVENTS = """SELECT i AS id, i % 50 AS customer_id, ['purchase', 'view', 'click'][1 + i % 3] AS event_type,
            (i % 97) * 1.5 AS amount, 'note-' || i AS note FROM range({n}) t(i)"""
CUSTOMERS = "SELECT i AS customer_id, ['US', 'MX', 'CA'][1 + i % 3] AS country FROM range(50) t(i)"
REVENUE = [{"fn": "sum", "column": "amount", "as": "revenue"}, {"fn": "count", "as": "events"}]


def write(path, sql):
    duckdb.sql(f"COPY ({sql}) TO '{path}'")
    return str(path)


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    return {"events": write(d / "events.parquet", EVENTS.format(n=20_000)),
            "small": write(d / "small.parquet", EVENTS.format(n=2_000)),
            "customers": write(d / "customers.csv", CUSTOMERS)}


@pytest.fixture
def paul(tmp_path):
    return Client(Runtime(tmp_path / "state"), "paul")


def call(handle, capability, input=None, **kw):
    """Invoke through the SDK and check the result against the protocol schema."""
    result = handle.call(capability, input, **kw)
    assert violations("results", result) == []
    return result


def code(result):
    assert result["status"] == "failed"
    return result["error"]["code"]


def revenue_by_country(events, customers):
    purchases = events.invoke("dataset.filter", {"where": [{"column": "event_type", "op": "eq", "value": "purchase"}]})
    joined = purchases.invoke("dataset.join", {"right": customers, "on": [{"left": "customer_id", "right": "customer_id"}]})
    totals = joined.invoke("dataset.aggregate", {"group_by": ["country"], "metrics": REVENUE})
    return totals.invoke("dataset.sort", {"by": [{"column": "country"}]})


def test_external_resource_gives_a_handle_and_no_contents(paul, data):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    assert violations("handles", events.info) == []
    text = json.dumps(events.info)
    assert "note-" not in text  # every row holds a "note-N" value
    assert data["events"] not in text and "paul" not in text  # no storage location, no principal
    derived = events.invoke("dataset.filter", {"where": [{"column": "id", "op": "lt", "value": 100}]})
    assert derived.info["kind"] == "computation" and "note-" not in json.dumps(derived.info)


def test_discovery_lists_only_the_permitted_capabilities(paul, data):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    events.grant("bob", ["dataset.count", "dataset.schema"])
    bob = Client(paul.runtime, "bob").resolve(events.id)
    assert sorted(bob.discover()) == ["dataset.count", "dataset.schema", "log.own"]
    assert "dataset.head" in events.discover()
    assert violations("handles", bob.info) == []


def test_large_input_gives_a_bounded_output(paul, data):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    result = call(events, "dataset.head", {"n": 20_000})
    assert result["output"]["truncated"] is True and 0 < len(result["output"]["rows"]) < 20_000
    assert result["metadata"]["bytes_transferred"] <= DEFAULT_MAX_OUTPUT_BYTES
    # An adapter that does not truncate gets a typed error from the runtime, and no data goes out.
    blob = paul.register("blob", adapter="memory", source={"value": {"text": "x" * (DEFAULT_MAX_OUTPUT_BYTES + 1)}})
    result = call(blob, "object.get")
    assert code(result) == "RESULT_TOO_LARGE" and "output" not in result


def test_an_operation_consumes_an_earlier_handle(paul, data):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    customers = paul.register("customers", adapter="duckdb", source=data["customers"])
    got = revenue_by_country(events, customers).invoke("dataset.head")
    want = duckdb.sql(f"""SELECT country, sum(amount)::DOUBLE, count(*) FROM '{data["events"]}' JOIN '{data["customers"]}'
                          USING (customer_id) WHERE event_type = 'purchase' GROUP BY country ORDER BY country""").fetchall()
    assert [tuple(r) for r in got["rows"]] == want and len(want) == 3


def test_a_graph_of_dependent_operations_runs_in_order(paul, data):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    customers = paul.register("customers", adapter="duckdb", source=data["customers"])
    top = revenue_by_country(events, customers)
    graph = top.graph()
    assert violations("graphs", graph) == []
    assert [n["kind"] for n in graph["nodes"]].count("operation") == 4
    # Submit the exported graph in reverse node order. The runtime must order it by the edges.
    graph["nodes"].reverse()
    run = paul.runtime.submit("paul", graph)
    assert all(r["status"] == "completed" for r in run["results"].values()) and len(run["results"]) == 4
    again = paul.resolve(run["handles"][top.id])
    assert again.id != top.id and again.invoke("dataset.head") == top.invoke("dataset.head")
    graph["edges"].append({"from": top.id, "to": graph["edges"][0]["to"]})
    with pytest.raises(HandleError) as e:
        paul.runtime.submit("paul", graph)
    assert e.value.code == "GRAPH_CYCLE"


def test_local_and_remote_execution_give_equal_results(paul, data, tmp_path):
    server = Worker(tmp_path / "worker", "the shared key", ["duckdb"]).serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    assert paul.runtime.add_worker("w1", url, "the shared key")["adapters"] == {"duckdb": {"persistence": "persistent"}}

    def ask(worker):
        events = paul.register("events", adapter="duckdb", source=data["events"], worker=worker)
        customers = paul.register("customers", adapter="duckdb", source=data["customers"], worker=worker)
        top = revenue_by_country(events, customers)
        stored = top.invoke("dataset.limit", {"n": 2}, eager=True)
        return events, top, [call(top, "dataset.head")["output"], top.invoke("dataset.count"), stored.invoke("dataset.head"),
                             {k: v for k, v in top.describe().items() if k not in ("id", "parents", "lifecycle")}]

    (near, _, local), (far, top, remote) = ask(None), ask("w1")
    assert local == remote and len(local[0]["rows"]) == 3
    # The same program ran in two places. The caller sees no place in a handle.
    assert violations("handles", far.info) == [] and url not in json.dumps([far.info, top.graph()])
    assert paul.runtime.registry.get(top.id).adapter == "duckdb@w1"  # a derived handle stays where its parents are
    # With no worker, the remote handle gives a typed error that a caller can retry. The local handle still works.
    server.shutdown()
    server.server_close()
    gone = call(top, "dataset.count")
    assert code(gone) == "WORKER_UNAVAILABLE" and gone["error"]["retryable"] is True
    assert near.invoke("dataset.count") == {"rows": 20_000}


class Clock(ResourceAdapter):
    """A resource type that the core does not know: a counter that a caller can read."""
    name = "clock"
    persistence = "ephemeral"
    ticks = 0

    def open(self, source):
        return ResourceRef("clock", {})

    def describe(self, ref):
        return {"description": "A tick counter."}

    def discover(self, ref):
        return [capability("clock.tick", "Add one tick and return the count.", write=True, idempotent=False),
                capability("clock.fail", "Raise a fault.")]

    def observe(self, ref):
        return {"version": str(self.ticks)}

    def invoke(self, ref, capability, input, *, eager, call):
        if capability == "clock.fail":
            raise RuntimeError("the clock is broken")
        self.ticks += 1
        return Value({"ticks": self.ticks}, str(self.ticks))


def test_a_new_adapter_works_with_no_change_to_the_core(tmp_path):
    paul = Client(Runtime(tmp_path, adapters=(Clock(),)), "paul")
    clock = paul.register("clock", adapter="clock", source={})
    assert clock.discover() == ["clock.tick", "clock.fail", "log.own", "log.audit"]
    assert clock.invoke("clock.tick") == {"ticks": 1} and clock.observe() == {"version": "1"}
    assert code(call(clock, "clock.tick", {"extra": 1})) == "INVALID_INPUT"


def test_output_size_does_not_grow_with_the_dataset(paul, data):
    sizes = {}
    for name in ("small", "events"):  # 2,000 rows and 20,000 rows
        events = paul.register(name, adapter="duckdb", source=data[name])
        totals = events.invoke("dataset.aggregate", {"group_by": ["event_type"], "metrics": [{"fn": "count", "as": "n"}]})
        result = call(totals, "dataset.head")
        assert sum(r[1] for r in result["output"]["rows"]) == {"small": 2_000, "events": 20_000}[name]
        sizes[name] = result["metadata"]["bytes_transferred"]
    assert sizes["events"] - sizes["small"] == 3  # one more digit in each of the three counts


def test_handles_and_history_survive_a_restart(tmp_path, data):
    paul = Client(Runtime(tmp_path), "paul")
    events = paul.register("events", adapter="duckdb", source=data["events"])
    lazy = events.invoke("dataset.aggregate", {"metrics": [{"fn": "max", "column": "id", "as": "top"}]})
    stored = lazy.invoke("dataset.limit", {"n": 1}, eager=True)
    scratch = paul.register("scratch", adapter="memory", source={"value": {}})
    want = stored.invoke("dataset.head")
    paul.runtime.registry.db.close()
    paul.runtime.adapter("duckdb").db.close()

    paul = Client(Runtime(tmp_path), "paul")
    assert paul.resolve("events").id == events.id
    assert paul.resolve(lazy.id).invoke("dataset.head") == want == paul.resolve(stored.id).invoke("dataset.head")
    assert paul.resolve(stored.id).info["kind"] == "result"
    log = paul.runtime.registry.history(result_handle=stored.id)
    assert [(r["capability"], r["status"], r["principal"]) for r in log] == [("dataset.limit", "completed", "paul")]
    with pytest.raises(HandleError) as e:  # a memory object does not live longer than its process
        paul.resolve(scratch.id)
    assert e.value.code == "HANDLE_NOT_FOUND"


def test_unauthorized_operations_are_rejected(paul, data, tmp_path):
    events = paul.register("events", adapter="duckdb", source=data["events"])
    customers = paul.register("customers", adapter="duckdb", source=data["customers"])
    bob = Client(paul.runtime, "bob")
    with pytest.raises(HandleError) as e:  # no grant: the handle id alone gives nothing
        bob.resolve(events.id)
    assert e.value.code == "HANDLE_NOT_FOUND"

    events.grant("bob", ["dataset.count", "dataset.filter", "dataset.join"])
    theirs = bob.resolve(events.id)
    assert theirs.invoke("dataset.count") == {"rows": 20_000}
    denied = call(theirs, "dataset.head")
    assert code(denied) == "CAPABILITY_DENIED" and "dataset.count" in denied["error"]["details"]["permitted"]
    # A derivation does not give more authority than its input.
    few = theirs.invoke("dataset.filter", {"where": [{"column": "id", "op": "lt", "value": 5}]})
    assert few.invoke("dataset.count") == {"rows": 5} and code(call(few, "dataset.head")) == "CAPABILITY_DENIED"
    # A second input needs a grant too.
    on = [{"left": "customer_id", "right": "customer_id"}]
    assert code(call(theirs, "dataset.join", {"right": customers, "on": on})) == "HANDLE_NOT_FOUND"
    customers.grant("bob", ["dataset.count"])
    assert code(call(theirs, "dataset.join", {"right": customers, "on": on})) == "CAPABILITY_DENIED"
    # A principal cannot give what it does not hold, and the owner can take a grant back.
    with pytest.raises(HandleError):
        theirs.grant("eve", ["dataset.head"])
    paul.runtime.revoke("paul", events.id, "bob")
    assert code(call(theirs, "dataset.count")) == code(call(few, "dataset.count")) == "HANDLE_NOT_FOUND"
    # The caller gives values, never SQL text.
    trick = events.invoke("dataset.filter", {"where": [{"column": "event_type", "op": "eq", "value": "x' OR 1=1 --"}]})
    assert trick.invoke("dataset.count") == {"rows": 0}
    assert code(call(events, "dataset.select", {"columns": ['id" FROM x; --']})) == "INVALID_INPUT"

    # A filesystem handle does not reach outside its root.
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("the secret")
    (root / "ok.txt").write_text("fine")
    os.symlink(tmp_path / "secret.txt", root / "link.txt")
    tree = paul.register("tree", adapter="filesystem", source=str(root))
    assert tree.invoke("filesystem.read", {"path": "ok.txt"})["text"] == "fine"
    for path in ("../secret.txt", "link.txt", str(tmp_path / "secret.txt")):
        assert code(call(tree, "filesystem.read", {"path": path})) == "CAPABILITY_DENIED"
    assert tree.invoke("filesystem.search", {"text": "secret"})["matches"] == []


def test_snapshot_and_latest_give_different_answers_after_a_change(paul, tmp_path):
    config = paul.register("config", adapter="memory", source={"value": {"mode": "old"}})
    pinned = config.invoke("object.snapshot")
    config.invoke("object.set", {"path": ["mode"], "value": "new"})
    old, new = call(pinned, "object.get", {"path": ["mode"]}), call(config, "object.get", {"path": ["mode"]})
    assert (old["output"], new["output"]) == ("old", "new")
    assert (pinned.info["consistency"], config.info["consistency"]) == ("snapshot", "latest")
    assert old["metadata"]["source_version"] != new["metadata"]["source_version"]

    path = write(tmp_path / "t.parquet", "SELECT 1 AS x")
    table = paul.register("t", adapter="duckdb", source=path)
    lazy = table.invoke("dataset.aggregate", {"metrics": [{"fn": "sum", "column": "x", "as": "total"}]})
    stored = table.invoke("dataset.aggregate", {"metrics": [{"fn": "sum", "column": "x", "as": "total"}]}, eager=True)
    write(path, "SELECT 2 AS x UNION ALL SELECT 3")
    assert lazy.invoke("dataset.head")["rows"] == [[5]] and stored.invoke("dataset.head")["rows"] == [[1]]
    assert stored.info["version"] and stored.info["version"] != table.observe()["version"]


def test_a_failed_operation_is_recorded_and_a_release_is_final(tmp_path, data):
    paul = Client(Runtime(tmp_path / "state", adapters=(Clock(),)), "paul")
    clock = paul.register("clock", adapter="clock", source={})
    failed = call(clock, "clock.fail")
    assert code(failed) == "ADAPTER_ERROR" and "broken" in failed["error"]["message"]
    assert clock.invoke("clock.tick") == {"ticks": 1}  # the runtime continues
    log = [r for r in paul.runtime.registry.history() if r["kind"] == "operation"]
    assert [(r["id"], r["status"], r["error_code"]) for r in log][0] == (failed["operation_id"], "failed", "ADAPTER_ERROR")

    events = paul.register("events", adapter="duckdb", source=data["events"])
    derived = events.invoke("dataset.limit", {"n": 3})
    stored = events.invoke("dataset.limit", {"n": 3}, eager=True)
    stored.release()
    assert code(call(stored, "dataset.count")) == "HANDLE_RELEASED" and derived.invoke("dataset.count") == {"rows": 3}
    events.release()
    assert code(call(derived, "dataset.count")) == code(call(events, "dataset.count")) == "HANDLE_RELEASED"
    assert os.path.getsize(data["events"]) > 0  # a release never deletes the external resource
    bad = paul.runtime.execute("paul", {"id": new_id("op"), "target": "hdl_" + "0" * 26, "capability": "clock.tick",
                                        "input": {}, "execution": {"mode": "auto"}})
    assert code(bad) == "HANDLE_NOT_FOUND" and violations("results", bad) == []
