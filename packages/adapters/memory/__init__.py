"""In-memory JSON objects. The objects live in the runtime process, so the handles are ephemeral."""
import copy

from ...core import HANDLE_INPUT, HandleError, capability, new_id
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

_PATH = {"type": "array", "items": {"type": ["string", "integer"]}, "description": "Keys and list indexes from the root of this handle."}
_CAPS = [
    capability("object.keys", "List the keys of a mapping, or give the length of a list.", {"path": _PATH}),
    capability("object.get", "Return the value at a path. The output size is limited.", {"path": _PATH}),
    capability("object.select", "Derive a handle to the value at a path. No data goes to the caller.",
               {"path": _PATH}, required=["path"], returns="handle"),
    capability("object.set", "Write a value at a path. The version of the object increases.",
               {"path": _PATH, "value": {}}, required=["path", "value"], write=True, idempotent=True),
    capability("object.equals", "Compare this object with a second object. The answer is one boolean.",
               {"other": HANDLE_INPUT}, required=["other"]),
    capability("object.snapshot", "Derive a handle to a copy of the object at its current version. Later writes do not change the copy.",
               returns="handle"),
]


class Adapter(ResourceAdapter):
    name = "memory"
    persistence = "ephemeral"

    def __init__(self, state_dir=None):
        self._objects: dict[str, list] = {}  # key -> [value, version]

    def lock_key(self, ref: ResourceRef):
        return ref.locator["key"]  # one object does not touch another

    def open(self, source: dict) -> ResourceRef:
        key = new_id("obj")
        self._objects[key] = [source["value"], 1]
        return ResourceRef("object", {"key": key, "path": []}, "1")

    def _slot(self, ref: ResourceRef) -> list:
        return self._objects[ref.locator["key"]]

    def _at(self, ref: ResourceRef, path=()):
        value = self._slot(ref)[0]
        for step in [*ref.locator["path"], *path]:
            try:
                value = value[step]
            except (KeyError, IndexError, TypeError):
                raise HandleError("INVALID_INPUT", f"no value at path step {step!r}") from None
        return value

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "An in-memory JSON object.", "schema": {"json_type": type(self._at(ref)).__name__}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        return {"version": str(self._slot(ref)[1])}

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        slot, path = self._slot(ref), input.get("path", [])
        version = str(slot[1])
        if capability == "object.get":
            return Value(self._at(ref, path), version)
        if capability == "object.keys":
            value = self._at(ref, path)
            if isinstance(value, dict):
                return Value({"keys": list(value)}, version)
            return Value({"length": len(value)} if isinstance(value, list) else {"scalar": type(value).__name__}, version)
        if capability == "object.select":
            self._at(ref, path)  # the path must exist now
            return Derived(ResourceRef("object", {"key": ref.locator["key"], "path": [*ref.locator["path"], *path]}))
        if capability == "object.set":
            if not path:
                raise HandleError("INVALID_INPUT", "object.set needs a path with one step or more")
            self._at(ref, path[:-1])[path[-1]] = input["value"]
            slot[1] += 1
            return Value({"version": str(slot[1])}, str(slot[1]))
        if capability == "object.equals":
            return Value({"equal": self._at(ref) == self._at(input["other"])}, version)
        if capability == "object.snapshot":
            # A copy is the only way to pin a version of a mutable Python object, so this always stores a result.
            key = new_id("obj")
            self._objects[key] = [copy.deepcopy(self._at(ref)), slot[1]]
            return Derived(ResourceRef("object", {"key": key, "path": []}, version), kind="result", consistency="snapshot")
        raise HandleError("CAPABILITY_NOT_FOUND", capability)

    def release(self, ref: ResourceRef, kind: str) -> None:
        if not ref.locator["path"]:  # a root object or a snapshot copy, both held by this adapter
            self._objects.pop(ref.locator["key"], None)
