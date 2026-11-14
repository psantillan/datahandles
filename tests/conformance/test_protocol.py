"""The protocol conformance suite (SPECIFICATION.md section 17).

Each test uses protocol methods only, through the client. It reads no state of an implementation.
Each handle and each result must pass the JSON Schemas in spec/protocol. See conftest.py for the targets.
"""
import json
import re

import pytest

from datahandles import HandleError
from datahandles.core import new_id, violations
from datahandles.core.wire import post

ID = re.compile(r"hdl_[0-9A-HJKMNP-TV-Z]{26}")


def call(handle, capability, input=None, **kw):
    result = handle.call(capability, input, **kw)
    assert violations("results", result) == []
    assert ("output" in result) + ("handles" in result) + ("error" in result) == 1
    return result


def code(result):
    assert result["status"] == "failed" and "output" not in result
    return result["error"]["code"]


def raised(fn, *args, **kw):
    with pytest.raises(HandleError) as e:
        fn(*args, **kw)
    return e.value.code


def test_a_handle_is_a_reference_with_no_contents(target):
    config = target.make("config", {"secret": "the-marker-value", "n": 1})
    info = config.describe()
    assert violations("handles", info) == [] and ID.fullmatch(config.id)
    assert (info["kind"], info["type"]) == ("resource", "object")
    assert "the-marker-value" not in json.dumps(info)
    assert target.paul.resolve("config").id == config.id and config.id in [h.id for h in target.paul.handles()]
    assert config.observe()["version"] == info["version"]
    # A value comes only from a capability that returns a value.
    assert call(config, "object.get", {"path": ["secret"]})["output"] == "the-marker-value"
    derived = call(config, "object.select", {"path": ["secret"]})["handles"][0]
    assert "the-marker-value" not in json.dumps(derived) and derived["parents"] == [config.id]
    assert all(isinstance(c, str) for c in derived["capabilities"])  # a result carries handle summaries


def test_discovery_lists_only_what_the_principal_holds(target):
    config, private = target.make("config", {"a": 1}), target.make("private", {"a": 2})
    assert {"object.get", "object.keys", "object.select", "object.set", "object.snapshot", "object.equals"} <= set(config.discover())
    config.grant(target.bob_name, ["object.keys", "object.get"])
    theirs = target.bob.resolve(config.id)
    assert sorted(theirs.discover()) == ["object.get", "object.keys"]
    assert violations("handles", theirs.info) == []
    assert [h.id for h in target.bob.handles()] == [config.id]


def test_a_handle_id_alone_gives_nothing(target):
    config = target.make("config", {"a": 1})
    errors = []
    for handle_id in (config.id, "hdl_" + "0" * 26):  # a handle of another principal, and an id that does not exist
        with pytest.raises(HandleError) as e:
            target.bob.resolve(handle_id)
        errors.append((e.value.code, e.value.message.replace(handle_id, "ID"), e.value.details))
    assert errors[0] == errors[1] and errors[0][0] == "HANDLE_NOT_FOUND"
    result = target.bob.runtime.execute(target.bob.principal, {"id": new_id("op"), "target": config.id, "capability": "object.get",
                                                             "input": {}, "execution": {"mode": "auto"}})
    assert violations("results", result) == [] and code(result) == "HANDLE_NOT_FOUND"


def test_each_refused_operation_gives_a_typed_error(target):
    config = target.make("config", {"a": 1})
    assert code(call(config, "object.explode")) == "CAPABILITY_NOT_FOUND"
    assert code(call(config, "object.get", {"extra": 1})) == "INVALID_INPUT"
    assert code(call(config, "object.select")) == "INVALID_INPUT"  # `path` is required
    assert code(call(config, "object.get", {"path": ["no", "such", "path"]})) == "INVALID_INPUT"
    config.grant(target.bob_name, ["object.keys"])
    denied = call(target.bob.resolve(config.id), "object.get")
    assert code(denied) == "CAPABILITY_DENIED" and denied["error"]["retryable"] is False
    not_an_operation = target.paul.runtime.execute(target.paul.principal, {"target": config.id})
    assert violations("results", not_an_operation) == [] and code(not_an_operation) == "INVALID_INPUT"
    assert config.invoke("object.get") == {"a": 1}  # no refused operation changed the resource


def test_a_derived_handle_gives_no_more_authority_than_its_parents(target):
    config = target.make("config", {"users": [{"name": "ana"}, {"name": "raj"}]})
    config.grant(target.bob_name, ["object.select", "object.keys"])
    theirs = target.bob.resolve(config.id)
    users = theirs.invoke("object.select", {"path": ["users"]})
    assert users.invoke("object.keys") == {"length": 2} and code(call(users, "object.get")) == "CAPABILITY_DENIED"
    assert raised(theirs.grant, "eve", ["object.get"]) == "CAPABILITY_DENIED"  # a principal gives only what it holds
    assert raised(theirs.revoke, "eve") == raised(theirs.release) == "CAPABILITY_DENIED"  # only the owner
    config.revoke(target.bob_name)
    assert code(call(theirs, "object.keys")) == code(call(users, "object.keys")) == "HANDLE_NOT_FOUND"


def test_a_second_input_handle_needs_the_same_capability(target):
    one, two = target.make("one", {"a": [1, 2]}), target.make("two", {"a": [1, 2]})
    assert one.invoke("object.equals", {"other": two}) == {"equal": True}
    assert one.invoke("object.equals", {"other": two.invoke("object.select", {"path": ["a"]})}) == {"equal": False}
    one.grant(target.bob_name, ["object.equals"])
    theirs = target.bob.resolve(one.id)
    assert code(call(theirs, "object.equals", {"other": {"$handle": two.id}})) == "HANDLE_NOT_FOUND"
    two.grant(target.bob_name, ["object.keys"])
    assert code(call(theirs, "object.equals", {"other": {"$handle": two.id}})) == "CAPABILITY_DENIED"
    two.grant(target.bob_name, ["object.equals"])
    assert theirs.invoke("object.equals", {"other": target.bob.resolve(two.id)}) == {"equal": True}


def test_an_output_over_the_limit_is_refused(target):
    small = target.make("small", {"text": "x"})
    limit = next(c for c in small.describe()["capabilities"] if c["name"] == "object.get")["limits"]["max_output_bytes"]
    big = target.make("big", {"text": "x" * limit})
    result = call(big, "object.get")
    assert code(result) == "RESULT_TOO_LARGE" and "x" * 100 not in json.dumps(result)
    assert call(big, "object.keys")["metadata"]["bytes_transferred"] < 100  # the question decides the size, not the resource


def test_a_snapshot_keeps_its_answers_after_a_write(target):
    config = target.make("config", {"mode": "old"})
    pinned = config.invoke("object.snapshot")
    config.invoke("object.set", {"path": ["mode"], "value": "new"})
    old, new = call(pinned, "object.get", {"path": ["mode"]}), call(config, "object.get", {"path": ["mode"]})
    assert (old["output"], new["output"]) == ("old", "new")
    assert (pinned.describe()["kind"], pinned.info["consistency"], config.describe()["consistency"]) == ("result", "snapshot", "latest")
    assert old["metadata"]["source_version"] != new["metadata"]["source_version"] == config.observe()["version"]


def test_a_release_ends_a_handle_and_each_handle_that_derives_from_it(target):
    config = target.make("config", {"a": {"b": 1}})
    inner, pinned = config.invoke("object.select", {"path": ["a"]}), config.invoke("object.snapshot")
    pinned.release()
    assert code(call(pinned, "object.get")) == "HANDLE_RELEASED" and inner.invoke("object.get") == {"b": 1}
    config.release()
    assert code(call(inner, "object.get")) == code(call(config, "object.get")) == "HANDLE_RELEASED"


def test_a_graph_is_exported_and_runs_in_dependency_order(target):
    config = target.make("config", {"a": {"b": {"c": 7}}})
    top = config.invoke("object.select", {"path": ["a"]}).invoke("object.select", {"path": ["b"]})
    graph = top.graph()
    assert violations("graphs", graph) == [] and [n["kind"] for n in graph["nodes"]].count("operation") == 2
    graph["nodes"].reverse()  # the order of the node list must not matter
    run = target.paul.submit(graph)
    assert [violations("results", r) for r in run["results"].values()] == [[], []]
    again = target.paul.resolve(run["handles"][top.id])
    assert again.id != top.id and again.invoke("object.get") == {"c": 7}
    graph["edges"].append({"from": top.id, "to": graph["edges"][0]["to"]})
    assert raised(target.paul.submit, graph) == "GRAPH_CYCLE"


def test_an_asynchronous_operation_is_a_job_handle(target):
    config = target.make("config", {"a": {"b": 1}})
    operation = {"id": new_id("op"), "target": config.id, "capability": "object.get", "input": {"path": ["a"]},
                 "execution": {"mode": "auto", "async": True}}
    accepted = target.paul.runtime.execute(target.paul.principal, operation)
    assert violations("results", accepted) == [] and accepted["status"] == "running" and "output" not in accepted
    job = target.paul.resolve(accepted["handles"][0]["id"])
    assert violations("handles", job.info) == [] and (job.info["kind"], job.info["type"]) == ("job", "job")
    assert sorted(job.discover()) == ["job.cancel", "job.result", "job.status"] and config.id in job.info["parents"]
    assert job.wait(timeout=30) == {"b": 1} == config.invoke("object.get", {"path": ["a"]})
    status = job.invoke("job.status")
    assert (status["state"], status["attempts"], status["operation_id"]) == ("completed", 1, operation["id"])
    assert job.invoke("job.cancel")["state"] == "completed"  # a final state does not change
    # The operation id is the idempotency key: the same submission gives the same job.
    assert target.paul.runtime.execute(target.paul.principal, operation)["handles"][0]["id"] == job.id
    assert code(target.paul.runtime.execute(target.paul.principal, {**operation, "input": {}})) == "INVALID_INPUT"
    # A job of a capability that returns a handle gives that handle. A job that fails gives the error of its operation.
    inner = config.invoke("object.select", {"path": ["a"]}, background=True).wait(timeout=30)
    assert inner.invoke("object.get") == {"b": 1}
    failed = config.invoke("object.get", {"path": ["nope"]}, background=True)
    assert raised(failed.wait, timeout=30) == "INVALID_INPUT" and failed.invoke("job.status")["state"] == "failed"
    # A job handle belongs to its submitter.
    assert raised(target.bob.resolve, job.id) == "HANDLE_NOT_FOUND"


def test_the_http_binding_takes_the_principal_from_the_token(target):
    if target.url is None:
        pytest.skip("this target does not use the HTTP transport")
    config = target.make("config", {"a": 1})

    def send(method, params, token):
        return post(target.url, "/v1/" + method, json.dumps(params).encode(), {"Authorization": f"Bearer {token}"} if token else {})

    assert send("handle.describe", {"handle": config.id}, target.token)[1]["result"]["id"] == config.id
    assert send("handle.describe", {"handle": config.id}, "not-a-token")[0] == 401
    assert send("handle.describe", {"handle": config.id}, None)[0] == 401
    assert send("handle.destroy", {"handle": config.id}, target.token)[0] == 404
    # A protocol error is a normal reply. A caller cannot name a principal in the request.
    status, reply = send("handle.describe", {"handle": "hdl_" + "0" * 26}, target.token)
    assert status == 200 and reply["error"]["code"] == "HANDLE_NOT_FOUND" and violations("errors", reply["error"]) == []
    status, reply = send("handle.describe", {"handle": config.id, "principal": "paul"}, target.token)
    assert status == 200 and reply["error"]["code"] == "INVALID_INPUT"
