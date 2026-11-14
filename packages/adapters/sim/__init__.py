"""A simulation of the room (packages/adapters/room) as a model: a digital twin.

A simulation state never changes. `sim.set` and `sim.step` derive a new state handle, so two
calls on one state with different inputs are two forks, and the state they start from stays.
The same input on the same state always gives an equal state (execution.deterministic).
The model is the physics of the room adapter, one step for each reading.
"""
from ...core import HandleError, capability, digest
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value
from ..room import HEAT, LOSS

_CAPS = [
    capability("sim.state", "Return the state: step, temperature, outside temperature, and the heater of the last step."),
    capability("sim.set", "Derive a state with another temperature, for example a measured one. The other values stay.",
               {"temperature": {"type": "number"}}, required=["temperature"], returns="handle", deterministic=True),
    capability("sim.step", "Derive the state after a number of steps with the heater on or off. One step is one room reading.",
               {"steps": {"type": "integer", "minimum": 1}, "heater": {"type": "boolean"}},
               required=["steps", "heater"], returns="handle", deterministic=True),
]


def _state(locator: dict) -> ResourceRef:
    return ResourceRef("sim_state", locator, digest(locator)[:16])


class Adapter(ResourceAdapter):
    name = "sim"

    def __init__(self, state_dir=None):
        pass

    def lock_key(self, ref: ResourceRef):
        return ref.version  # a state never changes, so calls on different states never meet

    def open(self, source: dict) -> ResourceRef:
        try:
            return _state({"step": 0, "temperature": float(source.get("temperature", 18.0)),
                           "outside": float(source.get("outside", 15.0)), "heater": False})
        except (TypeError, ValueError) as e:
            raise HandleError("INVALID_INPUT", f"temperature and outside must be numbers: {e}") from None

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "A state of the room simulation. It never changes; set and step derive new states.",
                "schema": {"step": ref.locator["step"]}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        return {"version": ref.version}

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        s = ref.locator
        if capability == "sim.state":
            return Value({**s, "temperature": round(s["temperature"], 3)}, ref.version)
        if capability == "sim.set":
            return Derived(_state({**s, "temperature": float(input["temperature"])}), kind="result", consistency="snapshot")
        if capability == "sim.step":
            temperature = s["temperature"]
            for _ in range(input["steps"]):
                call.check()
                temperature += LOSS * (s["outside"] - temperature) + (HEAT if input["heater"] else 0.0)
            new = {**s, "step": s["step"] + input["steps"], "temperature": temperature, "heater": input["heater"]}
            return Derived(_state(new), kind="result", consistency="snapshot")
        raise HandleError("CAPABILITY_NOT_FOUND", capability)
