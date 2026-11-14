"""The generic HTTP JSON API adapter: live requests, stored responses that replay, and the rules on hosts and tokens."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from datahandles import Client, HandleError, Runtime


class Service:
    """A local JSON API. The test changes `items` to show that a stored response does not change."""

    def __init__(self):
        self.items, self.release, self.auth = [{"id": 1, "name": "lamp"}, {"id": 2, "name": "desk"}], threading.Event(), []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body, headers=()):
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                service.auth.append(self.headers.get("Authorization"))
                path, _, query = self.path.partition("?")
                if path == "/v1/items":
                    return self.reply(200, {"items": service.items, "query": query})
                if path == "/v1/big":
                    return self.reply(200, {"rows": [{"n": i, "text": "x" * 50} for i in range(5000)]})
                if path == "/v1/slow":
                    service.release.wait(15)
                    return self.reply(200, {"late": True})
                if path == "/v1/moved":
                    return self.reply(302, {}, [("Location", "http://elsewhere.example/v1/items")])
                if path == "/v1/plain":
                    return self.reply(200, b"just text")
                self.reply(404, {"error": "no such path"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                service.items.append(body)
                self.reply(201, {"created": body})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def service():
    s = Service()
    yield s
    s.release.set()
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture
def paul(tmp_path):
    return Client(Runtime(tmp_path / "state"), "paul")


def test_a_get_returns_the_json_body_and_a_large_body_gives_its_shape(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    got = api.invoke("api.get", {"path": "/items", "query": {"limit": 2, "active": True}})
    assert got["status"] == 200 and got["json"]["items"][0]["name"] == "lamp" and got["json"]["query"] == "limit=2&active=true"
    big = api.invoke("api.get", {"path": "/big"})
    assert big["truncated"] and big["shape"] == {"keys": ["rows"], "key_count": 1} and "json" not in big
    assert api.invoke("api.get", {"path": "/plain"})["text"] == "just text"
    assert api.invoke("api.get", {"path": "/nothing"})["status"] == 404


def test_a_stored_response_never_changes_and_a_graph_over_it_replays(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    response = api.invoke("api.fetch", {"path": "/items"})
    assert response.info["kind"] == "result" and response.describe()["consistency"] == "snapshot"
    assert response.invoke("response.status")["status"] == 200
    assert response.invoke("response.keys") == {"keys": ["items", "query"], "key_count": 2}
    first = response.invoke("response.select", {"path": ["items", 0]})
    service.items[0]["name"] = "chair"  # the service changes
    assert first.invoke("response.get", {"path": ["name"]}) == "lamp"
    assert api.invoke("api.get", {"path": "/items"})["json"]["items"][0]["name"] == "chair"
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "r", "kind": "resource", "reference": response.id},
                       {"id": "op", "kind": "operation", "operation": {"capability": "response.get", "input": {"path": ["items", 1, "name"]}}}],
             "edges": [{"from": "r", "to": "op"}]}
    assert paul.submit(graph)["results"]["op"]["output"] == "desk"
    assert paul.submit(graph)["results"]["op"]["metadata"]["reused"]
    # A large body is read in parts.
    big = api.invoke("api.fetch", {"path": "/big"})
    assert big.invoke("response.keys", {"path": ["rows"]}) == {"length": 5000}
    assert big.invoke("response.get", {"path": ["rows", 4999, "n"]}) == 4999
    with pytest.raises(HandleError) as e:
        big.invoke("response.get")
    assert e.value.code == "RESULT_TOO_LARGE"


@pytest.mark.parametrize("path", ["http://evil.example/x", "//evil.example/x", "/../admin", "items", "/items?x=1"])
def test_a_path_cannot_leave_the_base_url(paul, service, path):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    with pytest.raises(HandleError) as e:
        api.invoke("api.get", {"path": path})
    assert e.value.code == "INVALID_INPUT"


def test_a_redirect_is_not_followed(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    moved = api.invoke("api.get", {"path": "/moved"})
    assert moved["status"] == 302 and moved["location"] == "http://elsewhere.example/v1/items"


def test_a_token_comes_from_the_environment_and_never_goes_into_the_registry(tmp_path, service, monkeypatch):
    monkeypatch.setenv("SHOP_TOKEN", "secret-value-123")
    paul = Client(Runtime(tmp_path / "state"), "paul")
    api = paul.register("shop", adapter="api", source={"base_url": service.url, "token_env": "SHOP_TOKEN"})
    api.invoke("api.get", {"path": "/items"})
    assert service.auth[-1] == "Bearer secret-value-123"
    assert b"secret-value-123" not in (tmp_path / "state" / "registry.sqlite").read_bytes()
    monkeypatch.delenv("SHOP_TOKEN")
    with pytest.raises(HandleError) as e:
        api.invoke("api.get", {"path": "/items"})
    assert e.value.code == "INVALID_INPUT"


def test_a_time_limit_stops_a_slow_request(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url, "timeout_ms": 300})
    t0 = time.perf_counter()
    result = api.call("api.get", {"path": "/slow"})
    assert result["error"]["code"] == "OPERATION_TIMEOUT" and time.perf_counter() - t0 < 5
    assert api.invoke("api.get", {"path": "/items"})["status"] == 200


def test_a_post_is_an_action_that_waits_for_an_approval_in_a_graph(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "api", "kind": "resource", "reference": api.id},
                       {"id": "order", "kind": "operation", "operation": {"capability": "api.post", "input": {"path": "/items", "json": {"id": 3}}}}],
             "edges": [{"from": "api", "to": "order"}]}
    g = paul.register("flow", adapter="graph", source={"graph": graph})
    assert g.invoke("graph.run")["results"]["order"]["status"] == "waiting" and len(service.items) == 2
    g.invoke("graph.approve", {"node": "order"})
    assert g.invoke("graph.run")["results"]["order"]["output"]["status"] == 201 and len(service.items) == 3


def test_a_response_larger_than_the_registered_limit_is_not_stored(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url, "max_response_bytes": 1000})
    with pytest.raises(HandleError) as e:
        api.invoke("api.fetch", {"path": "/big"})
    assert e.value.code == "RESULT_TOO_LARGE" and e.value.details == {"max_response_bytes": 1000}


def test_a_graph_never_reuses_an_answer_of_the_live_service(paul, service):
    api = paul.register("shop", adapter="api", source={"base_url": service.url})
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "api", "kind": "resource", "reference": api.id},
                       {"id": "get", "kind": "operation", "operation": {"capability": "api.get", "input": {"path": "/items"}}}],
             "edges": [{"from": "api", "to": "get"}]}
    paul.submit(graph)
    service.items.append({"id": 9, "name": "shelf"})
    again = paul.submit(graph)["results"]["get"]
    assert not again["metadata"].get("reused") and len(again["output"]["json"]["items"]) == 3
