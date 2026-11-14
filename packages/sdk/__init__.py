"""The Python SDK: a thin client over the runtime. It holds handle ids and builds Operation objects.

A chain of `invoke` calls makes a chain of handles. No resource data comes to the client until a
capability that returns a value is invoked.

The short form `events.filter(where=[...])` is the same as `events.invoke("dataset.filter", {"where": [...]})`:
the handle type gives the prefix, and the keyword arguments are the input.
"""
import time

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

    def call(self, capability: str, input: dict | None = None, *, eager: bool = False, background: bool = False) -> dict:
        """Invoke a capability and return the OperationResult object. A failure is a result, not an exception."""
        input = {k: {"$handle": v.id} if isinstance(v, Handle) else v for k, v in (input or {}).items()}
        execution = {"mode": "auto", "strategy": "eager" if eager else "lazy"}
        if background:
            execution["async"] = True
        return self._client.runtime.execute(self._client.principal, {
            "id": new_id("op"), "target": self.id, "capability": capability, "input": input, "execution": execution})

    def invoke(self, capability: str, input: dict | None = None, *, eager: bool = False, background: bool = False):
        """Invoke a capability. Return a Handle, or the bounded output value. Raise HandleError on a failure.

        `eager=True` asks the runtime to run a derivation now and store its output (materialization).
        `background=True` makes the operation a job. The return value is the job handle: see `wait`.
        """
        result = self.call(capability, input, eager=eager, background=background)
        if result["status"] == "failed":
            e = result["error"]
            raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
        return Handle(self._client, result["handles"][0]) if "handles" in result else result["output"]

    def __getattr__(self, verb: str):
        if verb.startswith("_") or "type" not in self.__dict__.get("info", ()):
            raise AttributeError(verb)
        return lambda **input: self.invoke(f"{self.info['type']}.{verb}", input)

    def wait(self, timeout: float | None = None, poll: float = 0.05):
        """Wait for the end of a job handle. Return what its operation gives: a Handle, or the bounded output value.

        Raise HandleError if the job failed or was cancelled. Raise TimeoutError after `timeout` seconds.
        poll: the seconds between two requests for the result.
        """
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self.invoke("job.result")
            except HandleError as e:
                if e.code != "JOB_NOT_FINISHED":
                    raise
            if end is not None and time.monotonic() >= end:
                raise TimeoutError(f"the job {self.id} did not end in {timeout} s")
            time.sleep(poll)

    def observe(self) -> dict:
        return self._client.runtime.observe(self._client.principal, self.id)

    def graph(self) -> dict:
        return self._client.runtime.graph(self._client.principal, self.id)

    def grant(self, to: str, capabilities: list[str]) -> None:
        self._client.runtime.grant(self._client.principal, self.id, to, capabilities)

    def revoke(self, principal: str) -> None:
        """Take back each grant that a principal has on this handle. Only the owner can do this."""
        self._client.runtime.revoke(self._client.principal, self.id, principal)

    def release(self) -> None:
        self._client.runtime.release(self._client.principal, self.id)


class Client:
    """One principal's view of a runtime.

    In-process transport: the caller vouches for the principal name. HTTP transport: the server
    takes the principal from the token, and `principal` here is not used.
    """

    def __init__(self, runtime, principal: str):
        self.runtime, self.principal = runtime, principal

    def register(self, name: str, *, adapter: str, source, worker: str | None = None) -> Handle:
        """Create a resource handle. `worker` names the remote worker that can reach the resource."""
        source = {"path": str(source)} if isinstance(source, str) else source
        return Handle(self, self.runtime.register(self.principal, name, adapter, source, worker))

    def submit(self, graph: dict) -> dict:
        """Run a computation graph (`Handle.graph` exports one). Return {"results": ..., "handles": {node id: handle id}}."""
        return self.runtime.submit(self.principal, graph)

    def handles(self) -> list[Handle]:
        """The live resource handles that this principal holds a capability on."""
        return [Handle(self, info) for info in self.runtime.handles(self.principal)]

    def resolve(self, name_or_id: str) -> Handle:
        if name_or_id.startswith("hdl_"):
            return Handle(self, self.runtime.describe(self.principal, name_or_id))
        return Handle(self, self.runtime.find(self.principal, name_or_id))


def connect(target=".datahandles", principal: str = "local", *, token: str | None = None) -> Client:
    """Return a client for one principal.

    target: a state directory, to start a local runtime in this process. Or the URL of a runtime
    behind the HTTP transport (`http://...`), with the bearer `token` of the principal.
    """
    if str(target).startswith(("http://", "https://")):
        from ..protocols.http import HttpRuntime
        return Client(HttpRuntime(str(target), token), principal)
    return Client(Runtime(target), principal)
