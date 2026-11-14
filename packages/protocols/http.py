"""The HTTP transport: the protocol methods of ARCHITECTURE.md section 7.1 as JSON over HTTP.

Request:  POST /v1/<method> with a JSON object of parameters and the header `Authorization: Bearer <token>`.
Reply:    HTTP 200 and {"result": ...}, or HTTP 200 and {"error": Error} for a protocol error.
          HTTP 401 when the token is not known. HTTP 404 when the method is not known.

The server takes the principal from the token. It never takes a principal from the request body.
A job has no method of its own: `job.status`, `job.cancel`, and `job.result` are capabilities of
the job handle, and a caller invokes them with `operation.invoke`.

Run a server:  python -m datahandles.protocols.http --state DIR --tokens FILE --port 8470
The tokens file is JSON: {"<token>": {"principal": "paul", "create": true}, ...}
"""
import argparse
import hashlib
import json
from pathlib import Path

from ..core import HandleError
from ..core.wire import post, post_events, serve

# method -> (the Runtime method, the parameter names in call order, the optional parameter names)
METHODS = {
    "handle.create": ("register", ("name", "adapter", "source"), ("worker",)),
    "handle.resolve": ("find", ("name",), ()),
    "handle.list": ("handles", (), ()),
    "handle.describe": ("describe", ("handle",), ("detail", "capability")),
    "handle.release": ("release", ("handle",), ()),
    "handle.grant": ("grant", ("handle", "to", "capabilities"), ()),
    "handle.revoke": ("revoke", ("handle", "from"), ()),
    "resource.observe": ("observe", ("handle",), ()),
    "operation.invoke": ("execute", ("operation",), ()),
    "operation.subscribe": ("subscribe", ("operation",), ()),
    "graph.export": ("graph", ("handle",), ()),
    "graph.submit": ("submit", ("graph",), ()),
}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Server:
    """Put one runtime behind HTTP.

    tokens: {token: principal}. creators: the principals that can call handle.create. That method makes
    the runtime (or a worker) open a location that the caller names, so it is off for each other principal.
    """

    def __init__(self, runtime, tokens: dict, creators=()):
        self.runtime, self.creators = runtime, set(creators)
        self._principals = {_digest(token): principal for token, principal in tokens.items()}

    def answer(self, path: str, headers, body: bytes) -> tuple[int, dict]:
        kind, _, token = (headers.get("Authorization") or "").partition(" ")
        principal = self._principals.get(_digest(token)) if kind == "Bearer" else None
        if principal is None:
            return 401, {"message": "no valid bearer token"}
        method = path.removeprefix("/v1/")
        if not path.startswith("/v1/") or method not in METHODS:
            return 404, {"message": f"no method at {path}"}
        name, required, optional = METHODS[method]
        try:
            try:
                params = json.loads(body or b"{}")
                if not isinstance(params, dict) or set(params) - set(required) - set(optional):
                    raise TypeError("unknown parameter")
                args = [params[k] for k in required] + [params[k] for k in optional if k in params]
            except (ValueError, KeyError, TypeError) as e:
                raise HandleError("INVALID_INPUT", f"{method} takes a JSON object with {list(required)} and optionally {list(optional)}: {type(e).__name__}: {e}")
            if method == "handle.create" and principal not in self.creators:
                raise HandleError("CAPABILITY_DENIED", "this principal cannot create resource handles over this transport")
            result = getattr(self.runtime, name)(principal, *args)
            if method == "operation.subscribe":
                return 200, _events(result)
            return 200, {"result": result}
        except HandleError as e:
            return 200, {"error": e.to_dict()}
        except Exception as e:  # a fault in one request must not stop the server
            return 500, {"message": f"{type(e).__name__}: {e}"}

    def serve(self, host: str = "127.0.0.1", port: int = 0):
        return serve(self.answer, host, port)


def _events(events):
    """Each event as {"result": event}. An error in the middle of the stream is the last event, as {"error": Error}."""
    try:
        for event in events:
            yield {"result": event}
    except HandleError as e:
        yield {"error": e.to_dict()}
    finally:
        events.close()


def _chain(first, rest):
    yield first
    yield from rest


class HttpRuntime:
    """The client side: an object with the methods of `Runtime` that sends each call to a server.

    Each method takes a principal first, to match `Runtime`. The value is not sent: the server knows
    the principal from the token.
    """

    def __init__(self, url: str, token: str):
        self.url, self._token = url.rstrip("/"), token

    def _call(self, method: str, **params):
        status, reply = post(self.url, "/v1/" + method, json.dumps(params).encode(), {"Authorization": f"Bearer {self._token}"})
        if status == 401:
            raise PermissionError(reply["message"])
        if status != 200:
            raise ConnectionError(f"HTTP {status} from {self.url}: {reply}")
        if "error" in reply:
            e = reply["error"]
            raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
        return reply["result"]

    def register(self, principal, name, adapter, source, worker=None):
        return self._call("handle.create", name=name, adapter=adapter, source=source, **({"worker": worker} if worker else {}))

    def find(self, principal, name):
        return self._call("handle.resolve", name=name)

    def handles(self, principal):
        return self._call("handle.list")

    def describe(self, principal, handle_id, detail="full", capability=None):
        return self._call("handle.describe", handle=handle_id, detail=detail, **({"capability": capability} if capability else {}))

    def release(self, principal, handle_id):
        return self._call("handle.release", handle=handle_id)

    def grant(self, principal, handle_id, to, capabilities):
        return self._call("handle.grant", handle=handle_id, to=to, capabilities=capabilities)

    def revoke(self, principal, handle_id, from_):
        return self._call("handle.revoke", **{"handle": handle_id, "from": from_})

    def observe(self, principal, handle_id):
        return self._call("resource.observe", handle=handle_id)

    def execute(self, principal, operation):
        return self._call("operation.invoke", operation=operation)

    def subscribe(self, principal, operation):
        events = post_events(self.url, "/v1/operation.subscribe", json.dumps({"operation": operation}).encode(),
                             {"Authorization": f"Bearer {self._token}"})
        first = next(events)
        if isinstance(first, tuple):  # a JSON reply: the subscription did not start
            events.close()
            status, reply = first
            if status == 401:
                raise PermissionError(reply["message"])
            if "error" in reply:
                e = reply["error"]
                raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
            raise ConnectionError(f"HTTP {status} from {self.url}: {reply}")
        return self._stream(first, events)

    @staticmethod
    def _stream(first, events):
        try:
            for item in _chain(first, events):
                if "error" in item:
                    e = item["error"]
                    raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
                yield item["result"]
        finally:
            events.close()

    def graph(self, principal, handle_id):
        return self._call("graph.export", handle=handle_id)

    def submit(self, principal, graph):
        return self._call("graph.submit", graph=graph)


def main() -> None:
    from ..runtime.runtime import Runtime
    p = argparse.ArgumentParser(description="Serve a DataHandles runtime over HTTP.")
    p.add_argument("--state", required=True, help="the state directory of the runtime")
    p.add_argument("--tokens", required=True, help='JSON file: {"TOKEN": {"principal": "NAME", "create": true}}')
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--worker", action="append", default=[], metavar="ID,URL,KEYFILE", help="register a remote worker; repeat for more workers")
    a = p.parse_args()
    tokens = json.loads(Path(a.tokens).read_text())
    runtime = Runtime(a.state)
    for spec in a.worker:
        worker_id, url, key_file = spec.split(",")
        runtime.add_worker(worker_id, url, Path(key_file).read_text().strip())
    server = Server(runtime, {t: v["principal"] for t, v in tokens.items()},
                    creators={v["principal"] for v in tokens.values() if v.get("create")}).serve(a.host, a.port)
    print(f"http://{a.host}:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
