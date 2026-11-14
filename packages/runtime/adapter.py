"""The contract between the runtime and a resource adapter (ARCHITECTURE.md section 5.1).

An adapter knows one kind of resource. It does not see principals, grants, or handle ids.
The runtime resolves a handle to a ResourceRef, does all checks, then calls the adapter.
"""
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Known:
    """A handle that is in the registry already. Only an adapter inside the runtime (the job adapter) returns it."""
    handle_id: str


class ResourceAdapter(ABC):
    name: str  # the adapter identifier in the registry
    persistence = "persistent"  # "ephemeral": the resource does not live longer than the runtime process

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
    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool) -> Value | Derived:
        """Do one operation. The input passed the input_schema; handle inputs are ResourceRef objects.

        For a capability that returns a handle: with eager False, return a plan and do not run it.
        With eager True, run it and store the output.
        """

    @abstractmethod
    def observe(self, ref: ResourceRef) -> dict:
        """Return the current state summary. It must include "version"."""

    def release(self, ref: ResourceRef, kind: str) -> None:
        """Free what the adapter holds for a released handle. Never delete the external resource."""


class Serial:
    """Let one thread at a time call an adapter. An adapter (for example one DuckDB connection) is not safe for two threads.

    `discover` does not wait. So during a long job on an adapter, a caller can still see capabilities and submit operations.
    """

    def __init__(self, adapter: ResourceAdapter):
        self._adapter, self._lock = adapter, threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._adapter, name)
        if not callable(attr) or name == "discover":
            return attr

        def call(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)
        return call
