"""A simulated room: a temperature sensor that makes a reading at a fixed interval, and a heater.

The room stands in for a device in the physical world. Its readings come one after another and
never come back (consistency `continuous`). Reading number `seq` is a fixed function of the
seed, the heater history, and `seq`, so a window of past readings never changes:

    temperature(seq + 1) = temperature(seq) + LOSS * (outside - temperature(seq)) + HEAT * heater
    reading = temperature + noise(seed, seq)

`room.record` stores a window of readings as a `result` handle (type `window`). A graph over a
window gives the same output on each replay, while the room goes on.
"""
import json
import random
import threading
import time

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HandleError, capability, new_id
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

# ASSUMED: the physics of the simulated room. One step is one reading. These numbers make a room
# that loses 2% of its difference to the outside air per step and gains 0.3 degrees per step from
# the heater. No measurement is behind them; they only need to give a plausible curve.
LOSS, HEAT, NOISE = 0.02, 0.3, 0.05
# Bytes kept for the JSON keys around a list in a truncated output.
_ENVELOPE = 256

_RANGE = {"from_seq": {"type": "integer", "minimum": 0}, "count": {"type": "integer", "minimum": 1}}
_ROOM_CAPS = [
    capability("room.latest", "Return the newest reading: seq, time in ms, temperature, heater."),
    capability("room.read", "Return the readings from_seq to from_seq + count - 1. The output stops at the size limit and says so.",
               _RANGE, required=["from_seq", "count"], deterministic=True),
    capability("room.record", "Record the readings from_seq to from_seq + count - 1 as a window handle. A window never changes.",
               _RANGE, required=["from_seq", "count"], returns="handle", deterministic=True),
    capability("room.heater", "Switch the heater of the room on or off. This changes the room.",
               {"on": {"type": "boolean"}}, required=["on"], write=True, external=True, idempotent=False),
    capability("room.readings", "Stream each new reading as it comes.", {"after_seq": {"type": "integer", "minimum": -1}}, mode="stream"),
]
_WINDOW_CAPS = [
    capability("window.stats", "Return the count, first and last seq, mean, minimum, and maximum temperature, and the heater share."),
    capability("window.read", "Return readings of the window. The output stops at the size limit and says so.",
               {"offset": {"type": "integer", "minimum": 0, "default": 0}, "n": {"type": "integer", "minimum": 1, "default": 20}}),
]


def _noise(seed: int, seq: int) -> float:
    return random.Random(seed * 1_000_003 + seq).uniform(-NOISE, NOISE)


class _Room:
    def __init__(self, interval_ms: int, seed: int, outside: float, start: float):
        self.interval_ms, self.seed, self.outside = interval_ms, seed, outside
        self.temperature, self.heater, self.readings = start, False, []
        self.changed, self.stop = threading.Condition(), threading.Event()
        threading.Thread(target=self._run, name="datahandles-room", daemon=True).start()

    def _run(self):
        while not self.stop.wait(self.interval_ms / 1000):
            with self.changed:
                seq = len(self.readings)
                self.readings.append({"seq": seq, "t_ms": seq * self.interval_ms, "heater": self.heater,
                                      "temperature": round(self.temperature + _noise(self.seed, seq), 3)})
                self.temperature += LOSS * (self.outside - self.temperature) + (HEAT if self.heater else 0.0)
                self.changed.notify_all()


def _fit(items: list, budget: int) -> list:
    kept = []
    for item in items:
        budget -= len(json.dumps(item).encode()) + 2
        if budget < 0:
            break
        kept.append(item)
    return kept


class Adapter(ResourceAdapter):
    name = "room"
    persistence = "ephemeral"
    consistency = "continuous"

    def __init__(self, state_dir=None):
        self._rooms: dict[str, _Room] = {}

    def lock_key(self, ref: ResourceRef):
        return ref.locator["room"]

    def open(self, source: dict) -> ResourceRef:
        interval = source.get("interval_ms")
        if not isinstance(interval, int) or interval < 1:
            raise HandleError("INVALID_INPUT", "a room needs interval_ms: the time between two readings, a positive integer")
        room_id = new_id("room")
        self._rooms[room_id] = _Room(interval, int(source.get("seed", 0)), float(source.get("outside", 15.0)), float(source.get("start", 18.0)))
        return ResourceRef("room", {"room": room_id}, "-1")

    def _room(self, ref: ResourceRef) -> _Room:
        room = self._rooms.get(ref.locator["room"])
        if room is None:
            raise HandleError("HANDLE_RELEASED", "the room ended")
        return room

    def describe(self, ref: ResourceRef) -> dict:
        if ref.type == "window":
            return {"description": "A recorded window of room readings. It never changes.",
                    "schema": {"from_seq": ref.locator["from_seq"], "count": len(ref.locator["readings"])}}
        room = self._room(ref)
        return {"description": "A simulated room with a temperature sensor and a heater.",
                "schema": {"interval_ms": room.interval_ms, "reading": ["seq", "t_ms", "temperature", "heater"]}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _WINDOW_CAPS if ref.type == "window" else _ROOM_CAPS

    def observe(self, ref: ResourceRef) -> dict:
        if ref.type == "window":
            return {"version": ref.version}
        room = self._room(ref)
        return {"version": str(len(room.readings) - 1), "heater": room.heater}

    def release(self, ref: ResourceRef, kind: str) -> None:
        if ref.type == "room":
            room = self._rooms.pop(ref.locator["room"], None)
            if room:
                room.stop.set()

    def _range(self, room: _Room, input: dict) -> list:
        first, count = input["from_seq"], input["count"]
        with room.changed:
            if first + count > len(room.readings):
                raise HandleError("INVALID_INPUT", f"readings {first} to {first + count - 1} are not all made yet",
                                  details={"latest_seq": len(room.readings) - 1})
            return room.readings[first:first + count]

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        budget = DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE
        if ref.type == "window":
            readings = ref.locator["readings"]
            if capability == "window.stats":
                temps = [r["temperature"] for r in readings]
                return Value({"count": len(readings), "first_seq": readings[0]["seq"], "last_seq": readings[-1]["seq"],
                              "mean": round(sum(temps) / len(temps), 3), "min": min(temps), "max": max(temps),
                              "heater_share": round(sum(r["heater"] for r in readings) / len(readings), 3)}, ref.version)
            part = readings[input.get("offset", 0):input.get("offset", 0) + input.get("n", 20)]
            kept = _fit(part, budget)
            return Value({"readings": kept, "truncated": len(kept) < len(part)}, ref.version)
        room = self._room(ref)
        if capability == "room.latest":
            with room.changed:
                if not room.readings:
                    raise HandleError("INVALID_INPUT", "no reading yet", retryable=True)
                return Value(room.readings[-1], str(room.readings[-1]["seq"]))
        if capability == "room.heater":
            with room.changed:
                room.heater = input["on"]
                return Value({"heater": room.heater, "from_seq": len(room.readings)}, str(len(room.readings) - 1))
        readings = self._range(room, input)
        if capability == "room.read":
            kept = _fit(readings, budget)
            return Value({"readings": kept, "truncated": len(kept) < len(readings)}, str(readings[-1]["seq"]))
        if capability == "room.record":
            version = f"{ref.locator['room']}:{input['from_seq']}:{input['count']}"
            window = ResourceRef("window", {"room": ref.locator["room"], "from_seq": input["from_seq"], "readings": readings}, version)
            return Derived(window, kind="result", consistency="snapshot")
        raise HandleError("CAPABILITY_NOT_FOUND", capability)

    def stream(self, ref: ResourceRef, capability: str, input: dict, call):
        """Give each reading after `after_seq` as it comes, until the call is cancelled."""
        room = self._room(ref)
        call.on_cancel(lambda: _wake(room))
        seq = input.get("after_seq", -1)
        while not call.cancelled and not room.stop.is_set():
            with room.changed:
                while len(room.readings) <= seq + 1 and not call.cancelled and not room.stop.is_set():
                    room.changed.wait()
                fresh = room.readings[seq + 1:]
            for reading in fresh:
                if call.cancelled:
                    return
                seq = reading["seq"]
                yield Value(reading, str(seq))


def _wake(room: _Room) -> None:
    with room.changed:
        room.changed.notify_all()
