"""An adapter from a separate installed package registers and runs with no change to datahandles."""
import importlib
import sys
import textwrap

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.workers.remote import Worker

ADAPTER = '''
from datahandles.core import capability
from datahandles.runtime.adapter import ResourceAdapter, ResourceRef, Value


class Adapter(ResourceAdapter):
    name = "counter"

    def __init__(self, state_dir):
        self.count = 0

    def open(self, source):
        return ResourceRef("counter", {}, "0")

    def describe(self, ref):
        return {"description": "A counter from an installed package."}

    def discover(self, ref):
        return [capability("counter.next", "Add one and return the count.", idempotent=False)]

    def observe(self, ref):
        return {"version": str(self.count)}

    def invoke(self, ref, capability, input, *, eager, call):
        self.count += 1
        return Value({"count": self.count}, str(self.count))
'''


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Put a package `counter_pkg` with an entry point in the group datahandles.adapters on sys.path."""
    site = tmp_path / "site"
    (site / "counter_pkg").mkdir(parents=True)
    (site / "counter_pkg" / "__init__.py").write_text(ADAPTER)
    info = site / "counter_pkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: counter-pkg\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(textwrap.dedent("""
        [datahandles.adapters]
        counter = counter_pkg:Adapter
        memory = counter_pkg:Adapter
    """))
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    yield
    sys.modules.pop("counter_pkg", None)


def test_an_installed_adapter_runs_in_the_runtime_and_on_a_worker(tmp_path, installed):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    counter = paul.register("c", adapter="counter", source={})
    assert counter.invoke("counter.next") == {"count": 1}
    assert counter.describe()["descriptor"]["description"] == "A counter from an installed package."
    assert "counter" in Worker(tmp_path / "worker", "key", ["counter"]).info()["adapters"]
    # A built-in name wins over an entry point with the same name.
    obj = paul.register("o", adapter="memory", source={"value": {"x": 1}})
    assert obj.invoke("object.get", {"path": ["x"]}) == 1


def test_an_adapter_that_nobody_installed_is_a_typed_error(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    with pytest.raises(HandleError) as e:
        paul.register("c", adapter="counter", source={})
    assert e.value.code == "INVALID_INPUT" and "datahandles.adapters" in e.value.message
