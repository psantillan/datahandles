"""The contract between the runtime and a resource adapter (ARCHITECTURE.md section 5.1).

An adapter knows one kind of resource. It does not see principals, grants, or handle ids.
The runtime resolves a handle to a ResourceRef, does all checks, then calls the adapter.
"""
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core import HandleError


@dataclass(frozen=True)
class ResourceRef:
    """What a handle points to, in the adapter's own terms. `locator` must be JSON data."""
    type: str
    locator: dict
    version: str = ""


@dataclass(frozen=True)
class Value:
    """A bounded observation that goes to the caller."""
    output: object
    version: str = ""


@dataclass(frozen=True)
class Derived:
    """A new reference. No resource data goes to the caller.

    kind "computation": the locator is a plan and nothing ran. kind "result": the output is stored.
    """
    ref: ResourceRef
    kind: str = "computation"
    consistency: str | None = None  # None: same as the target handle


class Call:
    """One adapter call. The runtime cancels it on `job.cancel` or when `limits.timeout_ms` passes.

    Python cannot stop a thread, so a cancel is cooperative. An adapter registers a hook with
    `on_cancel` (kill a subprocess, cancel a future) or checks `cancelled` between steps. Then its
    `invoke` returns or raises soon after the cancel, and its lock is free again.
    """

    def __init__(self):
        self._lock, self._hooks, self.reason = threading.Lock(), [], None

    @property
    def cancelled(self) -> bool:
        return self.reason is not None

    def on_cancel(self, hook) -> None:
        """Run `hook()` at the cancel. If the call is cancelled already, run it now."""
        with self._lock:
            if self.reason is None:
                self._hooks.append(hook)
                return
        hook()

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self.reason is not None:
                return
            self.reason, hooks, self._hooks = reason, self._hooks, []
        for hook in hooks:
            try:
                hook()
            except Exception:  # a hook that fails must not stop the other hooks
                pass

    def check(self) -> None:
        """Raise if the call is cancelled. An adapter calls this between the steps of a long operation."""
        if self.reason is not None:
            raise HandleError("JOB_CANCELLED" if self.reason == "cancel" else "OPERATION_TIMEOUT", f"the call was stopped: {self.reason}")


@dataclass(frozen=True)
class Known:
    """A handle that is in the registry already. Only an adapter inside the runtime (the job adapter) returns it."""
    handle_id: str


class ResourceAdapter(ABC):
    name: str  # the adapter identifier in the registry
    persistence = "persistent"  # "ephemeral": the resource does not live longer than the runtime process

    def lock_key(self, ref: ResourceRef):
        """Which calls must not run at the same time. Calls with different keys can run in parallel.

        The default None gives one lock for the whole adapter. That is correct for an adapter with
        one shared connection (DuckDB). An adapter whose resources are independent returns a key
        for each resource, for example the repository path.
        """
        return None

    @abstractmethod
    def open(self, source: dict) -> ResourceRef:
        """Check `source` and return the reference of the root resource."""

    @abstractmethod
    def describe(self, ref: ResourceRef) -> dict:
        """What is this thing? Return {"description": ..., "schema": ...}."""

    @abstractmethod
    def discover(self, ref: ResourceRef) -> list[dict]:
        """What can a caller do with it? Return capability descriptors.

        The runtime calls this from any thread, also during a long `invoke`. So it must not use state that `invoke` changes.
        """

    @abstractmethod
    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call: Call) -> Value | Derived:
        """Do one operation. The input passed the input_schema; handle inputs are ResourceRef objects.

        For a capability that returns a handle: with eager False, return a plan and do not run it.
        With eager True, run it and store the output.
        A long operation must stop soon after `call` is cancelled (see Call).
        """

    @abstractmethod
    def observe(self, ref: ResourceRef) -> dict:
        """Return the current state summary. It must include "version"."""

    def release(self, ref: ResourceRef, kind: str) -> None:
        """Free what the adapter holds for a released handle. Never delete the external resource."""


class Locked:
    """Let one thread at a time call an adapter for each lock key (see ResourceAdapter.lock_key).

    `discover` does not wait. So during a long job on a resource, a caller can still see capabilities and submit operations.
    `open` has no reference yet. It takes the lock of the whole adapter only when the adapter uses one lock for all.
    """

    def __init__(self, adapter: ResourceAdapter):
        self._adapter, self._guard, self._locks = adapter, threading.Lock(), {}

    def _lock(self, ref):
        key = None if ref is None else self._adapter.lock_key(ref)
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    def __getattr__(self, name):
        attr = getattr(self._adapter, name)
        if not callable(attr) or name in ("discover", "lock_key"):
            return attr

        def call(*args, **kwargs):
            ref = args[0] if args and isinstance(args[0], ResourceRef) else None
            if ref is None and self._adapter.lock_key.__func__ is not ResourceAdapter.lock_key:
                return attr(*args, **kwargs)  # `open` on an adapter with a lock for each resource
            with self._lock(ref):
                return attr(*args, **kwargs)
        return call
