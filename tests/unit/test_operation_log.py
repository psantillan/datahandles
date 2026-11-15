"""The operation log as two capabilities of each handle: log.own (your rows) and log.audit (each principal's rows)."""
import hashlib
import json
import sqlite3
import time

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.core import new_id
from datahandles.protocols.tools import dispatch
from datahandles.runtime.registry import Registry

USERS = {"users": [{"name": "ana"}, {"name": "raj"}], "region": "west"}


@pytest.fixture
def runtime(tmp_path):
    return Runtime(tmp_path / "state")


def rows(handle, cap="log.own", **input):
    return handle.invoke(cap, input)["rows"]


def test_a_principal_reads_its_own_rows_and_the_owner_reads_every_row(runtime):
    paul, bob = Client(runtime, "paul"), Client(runtime, "bob")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("bob", ["object.get"])
    mine = bob.resolve(config.id)
    mine.invoke("object.get", {"path": ["region"]})
    config.invoke("object.keys")
    assert [(r["principal"], r["capability"]) for r in rows(mine)] == [("bob", "object.get")]  # no grant needed for log.own
    audit = rows(config, "log.audit", kind="operation")
    assert [(r["principal"], r["capability"]) for r in audit] == [("bob", "object.get"), ("paul", "object.keys"), ("bob", "log.own")]
    with pytest.raises(HandleError) as e:
        mine.invoke("log.audit")
    assert e.value.code == "CAPABILITY_DENIED"


def test_the_audit_view_follows_the_parents_edges_to_handles_that_others_made(runtime):
    paul, bob = Client(runtime, "paul"), Client(runtime, "bob")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("bob", ["object.select", "object.get"])
    users = bob.resolve(config.id).invoke("object.select", {"path": ["users"]})  # bob owns the derived handle
    first = users.invoke("object.select", {"path": [0]})
    first.invoke("object.get", {"path": ["name"]})
    seen = rows(config, "log.audit", kind="operation")
    assert [(r["capability"], r["target"]) for r in seen] == [("object.select", config.id), ("object.select", users.id), ("object.get", first.id)]
    # The view of a descendant holds only its own part of the tree.
    assert [r["target"] for r in rows(Client(runtime, "paul").resolve(first.id), "log.audit", kind="operation")] == [first.id]


def test_an_owner_can_give_the_audit_view_alone(runtime):
    paul, bob, audit = Client(runtime, "paul"), Client(runtime, "bob"), Client(runtime, "auditor")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("bob", ["object.get"])
    bob.resolve(config.id).invoke("object.get")
    config.grant("auditor", ["log.audit"])
    view = audit.resolve(config.id)
    assert [r["principal"] for r in rows(view, "log.audit", kind="operation")] == ["bob"]
    with pytest.raises(HandleError) as e:
        view.invoke("object.get")
    assert e.value.code == "CAPABILITY_DENIED"


def test_a_handle_that_the_caller_holds_nothing_on_looks_like_one_that_does_not_exist(runtime):
    config = Client(runtime, "paul").register("config", adapter="memory", source={"value": USERS})
    for handle_id in (config.id, "hdl_" + "0" * 26):
        result = runtime.execute("eve", {"id": new_id("op"), "target": handle_id, "capability": "log.own", "input": {},
                                         "execution": {"mode": "auto", "strategy": "lazy"}})
        assert result["status"] == "failed" and result["error"]["code"] == "HANDLE_NOT_FOUND" and "details" not in result["error"]


def test_a_reader_of_the_graph_sees_which_node_failed_in_a_run_by_another_principal(runtime):
    paul, driver, selector = Client(runtime, "paul"), Client(runtime, "driver"), Client(runtime, "selector")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("driver", ["*"])
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "c", "kind": "resource", "reference": config.id},
                       {"id": "bad", "kind": "operation", "operation": {"capability": "object.get", "input": {"path": ["missing"]}}}],
             "edges": [{"from": "c", "to": "bad"}]}
    plan = driver.register("plan", adapter="graph", source={"graph": graph})
    plan.grant("selector", ["graph.set", "log.audit"])
    plan.invoke("graph.run")
    failed = rows(selector.resolve(plan.id), "log.audit", status="failed")
    run = [r for r in rows(selector.resolve(plan.id), "log.audit") if r.get("capability") == "graph.run"][0]
    assert [(r["principal"], r["capability"], r["error_code"], r["within"]) for r in failed] == [("driver", "object.get", "INVALID_INPUT", run["entry"])]
    # The edit and the run of the edited graph are in the same view: the edited graph is made from the first.
    edited = selector.resolve(plan.id).invoke("graph.set", {"node": "bad", "input": {"path": ["region"]}})
    driver.resolve(edited.id).invoke("graph.run")
    after = rows(selector.resolve(plan.id), "log.audit", after=run["entry"])
    assert [(r.get("capability"), r["status"]) for r in after] == [
        ("log.audit", "completed"), ("log.audit", "completed"), ("graph.set", "completed"), ("object.get", "completed"), ("graph.run", "completed")]
    assert after[2]["result_handle"] == edited.id  # the edit held: it made this graph


def test_an_action_that_waits_has_a_row(runtime):
    paul = Client(runtime, "paul")
    room = paul.register("room", adapter="room", source={"interval_ms": 50})
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "r", "kind": "resource", "reference": room.id},
                       {"id": "switch", "kind": "operation", "operation": {"capability": "room.heater", "input": {"on": True}}}],
             "edges": [{"from": "r", "to": "switch"}]}
    plan = paul.register("plan", adapter="graph", source={"graph": graph})
    assert plan.invoke("graph.run")["results"]["switch"]["status"] == "waiting"
    waiting = rows(plan, status="waiting")
    assert [(r["capability"], r["target"]) for r in waiting] == [("room.heater", room.id)]
    assert rows(room, capability="room.heater", status="completed") == []  # it did not run


def test_a_large_input_gives_its_first_bytes_and_the_hash_of_the_whole(runtime):
    paul = Client(runtime, "paul")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    big = {"path": ["region"], "value": "x" * 5000}
    for _ in range(2):
        config.call("object.set", big)
    found = rows(config, capability="object.set")
    canonical = json.dumps(big, sort_keys=True, separators=(",", ":"))
    assert all(r["truncated"] and "input" not in r and r["input_bytes"] == len(canonical) for r in found)
    assert found[0]["input_sha256"] == found[1]["input_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert found[0]["input_preview"] == canonical[:512]
    small = rows(config, capability="log.own")
    assert small and all("truncated" not in r for r in small)


def test_pages_follow_one_another_and_stay_under_the_size_limit(runtime):
    paul = Client(runtime, "paul")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    for i in range(120):  # about 100 KB of rows: more than one output limit
        config.call("object.set", {"path": ["region"], "value": f"{i}-" + "y" * 400})
    seen, after, pages = [], None, 0
    while True:
        pages += 1
        page = config.invoke("log.own", {"capability": "object.set", "limit": 200, **({"after": after} if after else {})})
        assert len(json.dumps(page).encode()) <= 65536
        seen += page["rows"]
        if "next" not in page:
            break
        after = page["next"]
    assert [r["input"]["value"].split("-")[0] for r in seen] == [str(i) for i in range(120)] and pages > 1  # the size limit ended a page
    assert all("seq" not in r for r in seen)
    small = config.invoke("log.own", {"capability": "object.set", "limit": 7})
    assert len(small["rows"]) == 7 and small["next"] == small["rows"][-1]["entry"]


def test_a_cursor_from_another_view_is_refused_like_one_that_does_not_exist(runtime):
    paul, bob = Client(runtime, "paul"), Client(runtime, "bob")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("bob", ["object.get"])
    config.invoke("object.keys")
    theirs = rows(config, capability="object.keys")[0]["entry"]
    mine = bob.resolve(config.id)
    for after in (theirs, "log_" + "0" * 26):
        with pytest.raises(HandleError) as e:
            mine.invoke("log.own", {"after": after})
        assert e.value.code == "INVALID_INPUT"


def test_lifecycle_methods_write_rows_also_when_they_fail(runtime):
    paul, bob = Client(runtime, "paul"), Client(runtime, "bob")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("bob", ["object.get"])
    with pytest.raises(HandleError):
        bob.resolve(config.id).grant("eve", ["object.set"])  # bob does not hold object.set
    runtime.revoke("paul", config.id, "bob")
    with pytest.raises(HandleError):
        runtime.release("bob", config.id)
    seen = [(r["kind"], r["principal"], r["status"], r.get("error_code")) for r in rows(config, "log.audit") if r["kind"] != "operation"]
    assert seen == [("create", "paul", "completed", None), ("grant", "paul", "completed", None), ("grant", "bob", "failed", "CAPABILITY_DENIED"),
                    ("revoke", "paul", "completed", None), ("release", "bob", "failed", "HANDLE_NOT_FOUND")]
    created = rows(config, "log.audit", kind="create")[0]
    assert created["result_handle"] == config.id and created["input"]["adapter"] == "memory" and "operation_id" not in created
    assert rows(config, "log.audit", kind="grant", status="failed")[0]["input"] == {"to": "eve", "capabilities": ["object.set"]}


def test_a_job_result_row_names_the_handle_that_the_job_made(runtime):
    paul = Client(runtime, "paul")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    job = config.invoke("object.snapshot", background=True)
    made = job.wait(timeout=10)
    results = rows(job, capability="job.result", status="completed")
    assert results and results[-1]["result_handle"] == made.id


def test_a_stream_row_ends_when_the_subscriber_stops(runtime):
    paul = Client(runtime, "paul")
    room = paul.register("room", adapter="room", source={"interval_ms": 5})
    events = room.subscribe("room.readings", {"after_seq": -1})
    next(events)
    assert rows(room, capability="room.readings")[0]["status"] == "running"
    events.close()
    ended = rows(room, capability="room.readings")[0]
    assert ended["status"] == "completed" and ended["duration_ms"] > 0 and ended["bytes_transferred"] > 0


def test_a_graph_never_reuses_a_read_of_the_log(runtime):
    paul = Client(runtime, "paul")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "c", "kind": "resource", "reference": config.id},
                       {"id": "log", "kind": "operation", "operation": {"capability": "log.own", "input": {"capability": "object.keys"}}}],
             "edges": [{"from": "c", "to": "log"}]}
    paul.submit(graph)
    config.invoke("object.keys")
    again = paul.submit(graph)["results"]["log"]
    assert not again["metadata"].get("reused") and len(again["output"]["rows"]) == 1


def test_a_model_reads_its_rows_through_the_handle_tool(runtime):
    paul, agent = Client(runtime, "paul"), Client(runtime, "agent")
    config = paul.register("config", adapter="memory", source={"value": USERS})
    config.grant("agent", ["object.get"])
    dispatch(agent, "handle_invoke", {"handle": config.id, "capability": "object.get", "input": {"path": ["nope"]}})
    out = dispatch(agent, "handle_invoke", {"handle": config.id, "capability": "log.own", "input": {"status": "failed"}})
    assert [r["error_code"] for r in out["output"]["rows"]] == ["INVALID_INPUT"]


def test_a_registry_from_v0_3_gets_the_new_log_columns(tmp_path):
    path = tmp_path / "registry.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE operations (seq INTEGER PRIMARY KEY, id TEXT NOT NULL, principal TEXT NOT NULL, target TEXT, capability TEXT,"
               " input TEXT, status TEXT NOT NULL, error_code TEXT, started_at TEXT NOT NULL, duration_ms REAL,"
               " bytes_transferred INTEGER, source_version TEXT, result_handle TEXT)")
    for i in range(3):
        db.execute("INSERT INTO operations (id, principal, status, started_at) VALUES (?, 'paul', 'completed', ?)", (f"op_{i}", time.time()))
    db.commit()
    db.close()
    old = Registry(path).history()
    assert [r["kind"] for r in old] == ["operation"] * 3 and len({r["entry"] for r in old}) == 3
    assert all(r["entry"].startswith("log_") and len(r["entry"]) == 30 for r in old)
    Registry(path).log(entry="log_new", id="op_new", principal="paul", status="completed", started_at="now", kind="operation")
