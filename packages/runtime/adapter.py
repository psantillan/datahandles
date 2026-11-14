"""The contract between the runtime and a resource adapter (ARCHITECTURE.md section 5.1).

An adapter knows one kind of resource. It does not see principals, grants, or handle ids.
The runtime resolves a handle to a ResourceRef, does all checks, then calls the adapter.
"""
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
        """What can a caller do with it? Return capability descriptors."""

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
