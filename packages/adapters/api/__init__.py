"""A generic HTTP JSON API. The principal that registers it names the base URL; a caller names only paths.

An `api` handle is a live service: it has no stable version, so a graph never reuses a result
from it. `api.fetch` stores one response as a `response` handle that never changes. A caller reads
it in parts (`response.get`, `response.keys`), and a graph over it replays exactly.

Rules:
- Each request goes to the scheme, host, and port of the base URL, under its path. A path with a
  scheme, a host, or a `..` segment is refused. The check reads the path as a server would after
  it decodes percent escapes, so `%2e%2e` is a `..` segment too.
- A redirect is not followed. The answer gives its status and its `location`.
- `api.fetch` stores only a 2xx response. Each other status is an error with the status in its details.
- A token never goes into the registry. `token_env` names an environment variable of the process
  that runs the adapter; its value goes in the header `Authorization: Bearer ...` of each request.
- `api.post` is an action (it writes, it reaches outside, it is not idempotent). In a graph it needs
  an approval.
"""
import json
import os
import socket
import time
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HandleError, capability, new_id
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

# ASSUMED: 10 MiB. The largest response body that `api.fetch` stores. No measurement is behind it;
# a registration can set another value with `max_response_bytes`.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
USER_AGENT = "datahandles (+https://github.com/psantillan/datahandles)"
# Bytes kept for the JSON keys around a value in a truncated output.
_ENVELOPE = 512

_PATH = {"type": "string", "description": "A path under the base URL, for example /v1/forecast."}
_QUERY = {"type": "object", "description": "Query parameters, for example {\"latitude\": 32.7}.",
          "additionalProperties": {"type": ["string", "number", "boolean", "array"]}}
_POINTER = {"type": "array", "items": {"type": ["string", "integer"]}, "description": "Keys and list indexes from the root of this response."}
_API_CAPS = [
    capability("api.get", "GET a path. Return the status and the JSON body. A body larger than the size limit gives its shape only: "
               "then use api.fetch and read the response in parts.", {"path": _PATH, "query": _QUERY}, required=["path"], external=True),
    capability("api.fetch", "GET a path and store the response as a handle that never changes. No body goes to the caller. "
               "A status that is not 2xx is an error, and nothing is stored.",
               {"path": _PATH, "query": _QUERY}, required=["path"], returns="handle", external=True),
    capability("api.post", "POST a JSON body to a path. This changes the service. A reply larger than the size limit gives its shape only.",
               {"path": _PATH, "json": {}},
               required=["path"], write=True, external=True, idempotent=False),
]
_RESPONSE_CAPS = [
    capability("response.status", "Return the HTTP status, the content type, the size, the URL, and the time of the fetch."),
    capability("response.keys", "List the keys of a mapping, or give the length of a list, at a path.", {"path": _POINTER}),
    capability("response.get", "Return the value at a path. The output size is limited.", {"path": _POINTER}),
    capability("response.select", "Derive a handle to the value at a path. No data goes to the caller.",
               {"path": _POINTER}, required=["path"], returns="handle"),
    capability("response.text", "Read a byte range of the body as text, for a body that is not JSON.",
               {"offset": {"type": "integer", "minimum": 0, "default": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_OUTPUT_BYTES, "default": 4096}}),
]


def _shape(value):
    if isinstance(value, dict):
        return {"keys": list(value)[:50], "key_count": len(value)}
    if isinstance(value, list):
        return {"length": len(value)}
    return {"type": type(value).__name__}


class Adapter(ResourceAdapter):
    name = "api"

    def __init__(self, state_dir=None):
        self._dir = Path(state_dir or ".") / "api"
        self._dir.mkdir(parents=True, exist_ok=True)

    def lock_key(self, ref: ResourceRef):
        return ref.locator.get("response") or ref.locator["base_url"]

    # ---- the service ----------------------------------------------------------------------------

    def open(self, source: dict) -> ResourceRef:
        base = urlsplit(source.get("base_url") or source.get("path", ""))  # a source given as one string arrives as `path`
        if base.scheme not in ("http", "https") or not base.netloc or base.query or base.fragment:
            raise HandleError("INVALID_INPUT", "base_url must be an http or https URL with a host and no query")
        timeout_ms, limit = source.get("timeout_ms"), source.get("max_response_bytes", MAX_RESPONSE_BYTES)
        for name, value in (("timeout_ms", timeout_ms), ("max_response_bytes", limit)):
            if value is not None and (not isinstance(value, int) or value < 1):
                raise HandleError("INVALID_INPUT", f"{name} must be a positive integer")
        if source.get("token_env") and not str(source["token_env"]).isidentifier():
            raise HandleError("INVALID_INPUT", "token_env must be the name of an environment variable")
        locator = {"base_url": f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}", "token_env": source.get("token_env"),
                   "timeout_ms": timeout_ms, "max_response_bytes": limit}
        return ResourceRef("api", locator, "")

    def _url(self, ref: ResourceRef, path: str, query: dict | None) -> tuple:
        # A server decodes percent escapes, and some servers read \ as /, before they resolve `..`.
        segments = unquote(path).replace("\\", "/").split("/")
        if (not path.startswith("/") or path.startswith("//") or "://" in path or "?" in path or "#" in path or ".." in segments
                or not all("!" <= c <= "~" for c in path)):
            raise HandleError("INVALID_INPUT", f"path {path!r} must start with / and have no host, no query, and no .. segment; "
                              "percent-encode a space or a character that is not ASCII", details={"base_url": ref.locator["base_url"]})
        base = urlsplit(ref.locator["base_url"])
        target = base.path + path
        if query:
            target += "?" + urlencode({k: json.dumps(v) if isinstance(v, bool) else v for k, v in query.items()}, doseq=True)
        return base, target

    def _request(self, ref: ResourceRef, method: str, path: str, query=None, body=None, call=None) -> dict:
        base, target = self._url(ref, path, query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if ref.locator["token_env"]:
            token = os.environ.get(ref.locator["token_env"])
            if not token:
                raise HandleError("INVALID_INPUT", f"the environment variable {ref.locator['token_env']} is not set in the process that runs the adapter")
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        # The cancel hook below cannot reach a socket that is not connected yet. So the time limit is
        # also the socket timeout: it ends a connect that gets no answer.
        timeout_ms = ref.locator["timeout_ms"]
        conn = (HTTPSConnection if base.scheme == "https" else HTTPConnection)(base.netloc, timeout=timeout_ms / 1000 if timeout_ms else None)

        def stop():  # a cancel or a time limit: wake the socket read
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        remove = call.on_cancel(stop) if call is not None else (lambda: None)
        limit = ref.locator["max_response_bytes"]
        try:
            conn.connect()
            if call is not None:
                call.check()  # a cancel came during the connect: send nothing
            conn.request(method, target, data, headers)
            reply = conn.getresponse()
            raw = reply.read(limit + 1)
            if len(raw) > limit:
                raise HandleError("RESULT_TOO_LARGE", f"the response is larger than max_response_bytes ({limit})", details={"max_response_bytes": limit})
            result = {"status": reply.status, "content_type": reply.getheader("Content-Type", ""), "url": f"{base.scheme}://{base.netloc}{target}",
                      "bytes": len(raw), "body": raw}
            if 300 <= reply.status < 400:
                result["location"] = reply.getheader("Location")
            return result
        except (OSError, HTTPException) as e:
            if call is not None:
                call.check()
            if isinstance(e, TimeoutError):
                raise HandleError("OPERATION_TIMEOUT", f"{method} {target}: no answer in {timeout_ms} ms", details={"timeout_ms": timeout_ms}) from e
            raise HandleError("ADAPTER_ERROR", f"{method} {target}: {type(e).__name__}: {e}", retryable=True) from e
        finally:
            remove()
            conn.close()

    @staticmethod
    def _json(result: dict):
        try:
            return json.loads(result["body"])
        except ValueError:
            return None

    def _answer(self, result: dict, hint: str) -> dict:
        """The bounded form of a live answer: the JSON body, or only its shape when it is over the size limit."""
        parsed = self._json(result)
        out = {"status": result["status"], "content_type": result["content_type"], "bytes": result["bytes"]}
        if "location" in result:
            out["location"] = result["location"]
        if parsed is None:
            out["text"] = result["body"][:2048].decode("utf-8", "replace")
        elif len(json.dumps(parsed).encode()) <= DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE:
            out["json"] = parsed
        else:
            out.update(truncated=True, shape=_shape(parsed), hint=hint)
        return out

    # ---- the stored responses --------------------------------------------------------------------

    def _stored(self, ref: ResourceRef) -> dict:
        try:
            return json.loads((self._dir / f"{ref.locator['response']}.json").read_text())
        except FileNotFoundError:
            raise HandleError("HANDLE_RELEASED", "the stored response is gone") from None

    def _at(self, ref: ResourceRef, path=()):
        value = self._stored(ref)["json"]
        if value is None:
            raise HandleError("INVALID_INPUT", "the body is not JSON: use response.text")
        for step in [*ref.locator["path"], *path]:
            try:
                value = value[step]
            except (KeyError, IndexError, TypeError):
                raise HandleError("INVALID_INPUT", f"no value at path step {step!r}") from None
        return value

    # ---- the contract ----------------------------------------------------------------------------

    def describe(self, ref: ResourceRef) -> dict:
        if ref.type == "response":
            s = self._stored(ref)
            return {"description": f"A stored response of GET {s['url']}. It never changes.",
                    "schema": {"status": s["status"], "content_type": s["content_type"], "path": ref.locator["path"]}}
        return {"description": f"An HTTP JSON API at {ref.locator['base_url']}.", "schema": {"base_url": ref.locator["base_url"]}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        if ref.type == "response":
            return _RESPONSE_CAPS
        caps = json.loads(json.dumps(_API_CAPS))
        if ref.locator["timeout_ms"]:  # the registration set a time limit for each request
            for cap in caps:
                cap["limits"] = {**cap.get("limits", {}), "timeout_ms": ref.locator["timeout_ms"]}
        return caps

    def observe(self, ref: ResourceRef) -> dict:
        # A service can change at any time, and the adapter cannot know. A new version on each observe
        # means a graph never reuses a result that came from the live service.
        return {"version": ref.version if ref.type == "response" else new_id("v")}

    def release(self, ref: ResourceRef, kind: str) -> None:
        if ref.type == "response" and not ref.locator["path"]:
            (self._dir / f"{ref.locator['response']}.json").unlink(missing_ok=True)

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        if ref.type == "response":
            if capability == "response.status":
                s = self._stored(ref)
                return Value({k: s[k] for k in ("status", "content_type", "bytes", "url", "fetched_at")}, ref.version)
            if capability == "response.text":
                body = self._stored(ref)["text"].encode()
                offset, length = input.get("offset", 0), input.get("length", 4096)
                part = body[offset:offset + length]
                return Value({"text": part.decode("utf-8", "replace"), "offset": offset, "bytes": len(part), "size": len(body),
                              "eof": offset + len(part) >= len(body)}, ref.version)
            value = self._at(ref, input.get("path", []))
            if capability == "response.keys":
                return Value(_shape(value), ref.version)
            if capability == "response.get":
                return Value(value, ref.version)  # the runtime enforces the size limit
            if capability == "response.select":
                return Derived(ResourceRef("response", {**ref.locator, "path": [*ref.locator["path"], *input["path"]]}, ref.version),
                               kind="computation", consistency="snapshot")
            raise HandleError("CAPABILITY_NOT_FOUND", capability)
        if capability == "api.post":
            # The service changed already, so a large reply must not become an error: give its shape.
            result = self._request(ref, "POST", input["path"], body=input.get("json"), call=call)
            return Value(self._answer(result, "the reply is larger than the size limit: only its shape is given"), "")
        result = self._request(ref, "GET", input["path"], input.get("query"), call=call)
        if capability == "api.get":
            return Value(self._answer(result, "the body is larger than the size limit: use api.fetch and read it in parts"), "")
        if capability == "api.fetch":
            if not 200 <= result["status"] < 300:
                raise HandleError("ADAPTER_ERROR", f"GET {result['url']} gave the status {result['status']}: api.fetch stores only a 2xx response",
                                  retryable=result["status"] == 429 or result["status"] >= 500,
                                  details={"status": result["status"], "location": result.get("location"),
                                           "text": result["body"][:2048].decode("utf-8", "replace")})
            parsed = self._json(result)
            response_id = new_id("rsp")
            stored = {"status": result["status"], "content_type": result["content_type"], "bytes": result["bytes"], "url": result["url"],
                      "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "json": parsed,
                      "text": result["body"].decode("utf-8", "replace") if parsed is None else ""}
            (self._dir / f"{response_id}.json").write_text(json.dumps(stored))
            return Derived(ResourceRef("response", {"response": response_id, "path": []}, response_id), kind="result", consistency="snapshot")
        raise HandleError("CAPABILITY_NOT_FOUND", capability)
