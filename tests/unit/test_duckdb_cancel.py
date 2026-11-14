"""A cancel interrupts a running DuckDB query."""
import time

import pytest

pytest.importorskip("duckdb")
from datahandles import Client, HandleError, Runtime  # noqa: E402


def test_a_cancel_stops_a_running_query(tmp_path):
    (tmp_path / "left.csv").write_text("k,v\n" + "".join(f"1,{i}\n" for i in range(80000)))
    (tmp_path / "right.csv").write_text("k,w\n" + "".join(f"1,{i}\n" for i in range(80000)))
    paul = Client(Runtime(tmp_path / "state"), "paul")
    rows = paul.register("left", adapter="duckdb", source=str(tmp_path / "left.csv"))
    other = paul.register("right", adapter="duckdb", source=str(tmp_path / "right.csv"))
    # A join on a column with one value: 6.4 billion rows. Without a cancel it runs about 20 s (30,000 rows each: 3.3 s, measured).
    pairs = rows.invoke("dataset.join", {"right": other, "on": [{"left": "k", "right": "k"}]})
    job = pairs.invoke("dataset.count", background=True)
    time.sleep(0.5)
    assert job.invoke("job.status")["state"] == "running"
    t0 = time.perf_counter()
    job.invoke("job.cancel")
    with pytest.raises(HandleError) as e:
        job.wait(timeout=30)
    assert e.value.code == "JOB_CANCELLED" and time.perf_counter() - t0 < 3
    assert rows.invoke("dataset.count") == {"rows": 80000}  # the connection works after the interrupt
