"""The Python SDK: a thin client over the runtime. It holds handle ids and builds Operation objects.

A chain of `invoke` calls makes a chain of handles. No resource data comes to the client until a
capability that returns a value is invoked.
"""
from ..core import HandleError, new_id
from ..runtime.runtime import Runtime


class Handle:
    def __init__(self, client: "Client", info: dict):
        # `info` is a protocol Handle, or the HandleSummary from an OperationResult until `describe` is called.
        self._client, self.info, self.id = client, info, info["id"]

    def __repr__(self) -> str:
        return f"<Handle {self.id} {self.info['kind']} {self.info['type']}>"

    def describe(self) -> dict:
        """Return the protocol Handle object: what the resource is and which capabilities this principal holds."""
        self.info = self._client.runtime.describe(self._client.principal, self.id)
        return self.info

    def discover(self) -> list[str]:
        return [c["name"] for c in self.describe()["capabilities"]]

    def call(self, capability: str, input: dict | None = None, *, eager: bool = False) -> dict:
        """Invoke a capability and return the OperationResult object. A failure is a result, not an exception."""
        input = {k: {"$handle": v.id} if isinstance(v, Handle) else v for k, v in (input or {}).items()}
        return self._client.runtime.execute(self._client.principal, {
            "id": new_id("op"), "target": self.id, "capability": capability, "input": input,
            "execution": {"mode": "auto", "strategy": "eager" if eager else "lazy"}})

    def invoke(self, capability: str, input: dict | None = None, *, eager: bool = False):
        """Invoke a capability. Return a Handle, or the bounded output value. Raise HandleError on a failure.

        `eager=True` asks the runtime to run a derivation now and store its output (materialization).
        """
        result = self.call(capability, input, eager=eager)
        if result["status"] != "completed":
            e = result["error"]
            raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
        return Handle(self._client, result["handles"][0]) if "handles" in result else result["output"]

    def observe(self) -> dict:
        return self._client.runtime.observe(self._client.principal, self.id)

    def graph(self) -> dict:
        return self._client.runtime.graph(self._client.principal, self.id)

    def grant(self, to: str, capabilities: list[str]) -> None:
        self._client.runtime.grant(self._client.principal, self.id, to, capabilities)

    def release(self) -> None:
        self._client.runtime.release(self._client.principal, self.id)


class Client:
    """One principal's view of a runtime. In-process transport: the caller vouches for the principal name."""

    def __init__(self, runtime: Runtime, principal: str):
        self.runtime, self.principal = runtime, principal

    def register(self, name: str, *, adapter: str, source) -> Handle:
        source = {"path": str(source)} if isinstance(source, str) else source
        return Handle(self, self.runtime.register(self.principal, name, adapter, source))

    def resolve(self, name_or_id: str) -> Handle:
        if name_or_id.startswith("hdl_"):
            return Handle(self, self.runtime.describe(self.principal, name_or_id))
        return Handle(self, self.runtime.find(self.principal, name_or_id))


def connect(state_dir=".datahandles", principal: str = "local") -> Client:
    """Start a local runtime in this process and return a client for one principal."""
    return Client(Runtime(state_dir), principal)
