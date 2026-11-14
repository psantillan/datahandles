"""A wrong handle id gets a correction, and the correction never names a handle the caller cannot use."""
from datahandles import Client, Runtime
from datahandles.core import edit_distance
from datahandles.protocols.tools import dispatch


def test_a_wrong_id_gets_the_near_ids_that_the_principal_holds(tmp_path):
    owner = Client(Runtime(tmp_path), "owner")
    shared = owner.register("shared", adapter="memory", source={"value": {"a": {"x": 1}}})
    private = owner.register("private", adapter="memory", source={"value": {"b": 2}})
    shared.grant("agent", ["object.get"])
    agent = Client(owner.runtime, "agent")

    def suggestion(client, text):
        error = dispatch(client, "handle_invoke", {"handle": text, "capability": "object.get", "input": {"path": []}})["error"]
        assert error["code"] == "HANDLE_NOT_FOUND"
        return error.get("details", {}).get("did_you_mean")

    assert edit_distance("kitten", "sitting") == 3 and edit_distance("", "abc") == 3 and edit_distance("abc", "abc") == 0
    dropped = shared.id[:12] + shared.id[13:]
    i = next(i for i in range(4, 29) if shared.id[i] != shared.id[i + 1])  # two neighbors that differ
    swapped = shared.id[:i] + shared.id[i + 1] + shared.id[i] + shared.id[i + 2:]
    for wrong in (dropped, swapped, shared.id + "X", "shared: " + shared.id):
        assert wrong != shared.id and suggestion(agent, wrong) == [shared.id], wrong
    assert suggestion(agent, shared.id[:9] + shared.id[12:]) is None  # 3 edits: too far to offer

    # No leak: the agent holds nothing on `private`, so a near miss gets the plain error.
    near_private = private.id[:12] + private.id[13:]
    assert suggestion(agent, near_private) is None
    assert suggestion(owner, near_private) == [private.id]

    derived = shared.invoke("object.select", {"path": ["a"]})
    near_derived = derived.id[:12] + derived.id[13:]
    assert suggestion(owner, near_derived) == [derived.id]
    shared.release()
    # A released handle is not offered, and a handle derived from it is not offered.
    assert suggestion(owner, dropped) is None and suggestion(owner, near_derived) is None
