"""The browser adapter: a session with long, non-idempotent actions. Cancel and the time limit stop them."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from datahandles import Client, HandleError, Runtime  # noqa: E402

PAGES = {
    "/": "<title>Home</title><p>Welcome to the test site.</p><a href='/next'>Next page</a>"
         "<input id='name'><p id='out'></p>"
         "<button id='go' onclick=\"document.getElementById('out').innerText = 'Hello ' + document.getElementById('name').value\">Go</button>",
    "/next": "<title>Next</title><p>The second page.</p>",
}


class Site:
    """A local web site. /slow does not answer until the test releases it."""

    def __init__(self):
        self.slow_entered, self.release = threading.Event(), threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/slow":
                    site.slow_entered.set()
                    site.release.wait(15)
                body = PAGES.get(self.path, "<title>Slow</title>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.release.set()
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture
def paul(tmp_path):
    runtime = Runtime(tmp_path / "state")
    yield Client(runtime, "paul")
    runtime.adapter("browser").close()


def test_a_session_handle_opens_pages_reads_them_and_acts_on_them(paul, site):
    s = paul.register("web", adapter="browser", source={})
    assert s.info["type"] == "browser_session" and s.info["lifecycle"]["persistence"] == "ephemeral"
    assert s.invoke("browser.goto", {"url": site.url + "/"}) == {"url": site.url + "/", "title": "Home", "status": 200}
    assert "Welcome to the test site." in s.invoke("browser.text")["text"]
    assert s.invoke("browser.links")["links"] == [{"text": "Next page", "href": site.url + "/next"}]
    s.invoke("browser.fill", {"selector": "#name", "value": "Paul"})
    s.invoke("browser.click", {"selector": "#go"})
    assert s.invoke("browser.text", {"selector": "#out"})["text"] == "Hello Paul"
    assert s.invoke("browser.click", {"selector": "text=Next page"})["title"] == "Next"
    assert s.observe()["url"] == site.url + "/next"
    # The capabilities that change the page are not idempotent, so a restart does not run them again.
    goto = next(c for c in s.describe()["capabilities"] if c["name"] == "browser.goto")
    assert goto["execution"]["idempotent"] is False and goto["effects"]["external"] is True


def test_cancel_stops_a_navigation_that_runs_as_a_job(paul, site):
    s = paul.register("web", adapter="browser", source={})
    job = s.invoke("browser.goto", {"url": site.url + "/slow"}, background=True)
    assert site.slow_entered.wait(10)
    t0 = time.perf_counter()
    job.invoke("job.cancel")
    deadline = time.time() + 5
    while job.invoke("job.status")["state"] != "cancelled" and time.time() < deadline:
        time.sleep(0.05)
    assert job.invoke("job.status")["state"] == "cancelled"
    assert time.perf_counter() - t0 < 5 and not site.release.is_set()  # the page never answered
    assert s.invoke("browser.goto", {"url": site.url + "/next"})["title"] == "Next"  # the session is free again


def test_a_session_with_a_time_limit_stops_a_slow_action(paul, site):
    s = paul.register("web", adapter="browser", source={"timeout_ms": 500})
    t0 = time.perf_counter()
    result = s.call("browser.goto", {"url": site.url + "/slow"})
    assert result["error"]["code"] == "OPERATION_TIMEOUT" and time.perf_counter() - t0 < 5
    assert s.invoke("browser.goto", {"url": site.url + "/"})["title"] == "Home"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi"])
def test_a_session_opens_only_http_and_https(paul, url):
    s = paul.register("web", adapter="browser", source={})
    with pytest.raises(HandleError) as e:
        s.invoke("browser.goto", {"url": url})
    assert e.value.code == "INVALID_INPUT"
