"""The targets of the conformance suite.

With no setting, the suite runs on the reference implementation in three arrangements:
  in-process  the Python API of the runtime
  http        the same runtime behind the HTTP transport, on a real socket
  worker      the fixture resources on a remote worker

To test another implementation, give the suite its HTTP endpoint. Then only that target runs:
  DATAHANDLES_URL          the base URL of the runtime, for example http://127.0.0.1:8470
  DATAHANDLES_TOKEN        the bearer token of a principal that can call handle.create
  DATAHANDLES_TOKEN_2      the bearer token of a second principal
  DATAHANDLES_PRINCIPAL_2  the name of the second principal in a grant (default: bob)
"""
import os
import threading
from dataclasses import dataclass

import pytest

from datahandles import Client, Runtime, connect
from datahandles.protocols.http import Server
from datahandles.workers.remote import Worker

EXTERNAL = os.environ.get("DATAHANDLES_URL")


@dataclass
class Target:
    name: str
    paul: Client  # the owner of each fixture resource
    bob: Client  # a second principal. It holds nothing until the owner grants it.
    bob_name: str
    worker: str | None = None
    url: str | None = None  # set when the clients use the HTTP transport
    token: str | None = None

    def make(self, name: str, value):
        """Create the fixture resource of the suite: a handle of the type `object` on a JSON value (SPECIFICATION.md section 14)."""
        return self.paul.register(name, adapter="memory", source={"value": value}, worker=self.worker)


def _serve(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


@pytest.fixture(params=["external"] if EXTERNAL else ["in-process", "http", "worker"])
def target(request, tmp_path):
    servers = []
    if request.param == "external":
        token = os.environ["DATAHANDLES_TOKEN"]
        yield Target("external", connect(EXTERNAL, token=token), connect(EXTERNAL, token=os.environ["DATAHANDLES_TOKEN_2"]),
                     os.environ.get("DATAHANDLES_PRINCIPAL_2", "bob"), url=EXTERNAL, token=token)
        return
    runtime = Runtime(tmp_path / "state")
    if request.param == "http":
        servers.append(Server(runtime, {"token-paul": "paul", "token-bob": "bob"}, creators={"paul"}).serve())
        url = _serve(servers[0])
        yield Target("http", connect(url, token="token-paul"), connect(url, token="token-bob"), "bob", url=url, token="token-paul")
    else:
        worker = None
        if request.param == "worker":
            servers.append(Worker(tmp_path / "worker", "the shared key", ["memory"]).serve())
            runtime.add_worker("w1", _serve(servers[0]), "the shared key")
            worker = "w1"
        yield Target(request.param, Client(runtime, "paul"), Client(runtime, "bob"), "bob", worker=worker)
    for server in servers:
        server.shutdown()
        server.server_close()
