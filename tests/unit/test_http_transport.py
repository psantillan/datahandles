"""The HTTP server of the reference runtime: what it adds to the protocol binding (the conformance suite tests the binding)."""
import threading

import pytest

from datahandles import HandleError, Runtime, connect
from datahandles.protocols.http import Server


def test_only_a_listed_principal_can_create_a_resource_handle(tmp_path):
    (tmp_path / "docs").mkdir()
    server = Server(Runtime(tmp_path / "state"), {"token-paul": "paul", "token-bob": "bob"}, creators={"paul"}).serve()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    # handle.create makes the runtime open a location that the caller names. So it is not for each token holder.
    with pytest.raises(HandleError) as e:
        connect(url, token="token-bob").register("docs", adapter="filesystem", source=str(tmp_path / "docs"))
    assert e.value.code == "CAPABILITY_DENIED"
    docs = connect(url, token="token-paul").register("docs", adapter="filesystem", source=str(tmp_path / "docs"))
    assert docs.list() == docs.invoke("filesystem.list") and docs.info["type"] == "filesystem"  # the short call form
    with pytest.raises(PermissionError):
        connect(url, token="token-eve").resolve("docs")
    server.shutdown()
    server.server_close()
