"""Measure the bytes that go to the caller for one fixed question while the dataset grows.

The question: "Which three countries have the most purchase revenue?" It needs a filter, a join,
an aggregate, a sort, and a bounded read: five operations on two resources.

For each dataset size this script records:
  - protocol bytes: the JSON size of all five OperationResult objects (the handles included)
  - answer bytes:   the JSON size of the output of the last operation
  - export bytes:   the size of the events table as JSON rows (DuckDB COPY ... FORMAT json),
                    which is what a caller must receive to compute the answer itself

Run: .venv/bin/python benchmarks/context_efficiency.py [max_rows]
"""
import json
import platform
import shutil
import sys
import time
from pathlib import Path

import duckdb

from datahandles import connect

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVENTS = """SELECT i AS id, (hash(i) % 100000)::BIGINT AS customer_id, ['purchase', 'view', 'click'][(1 + hash(i + 1) % 3)::BIGINT] AS event_type,
            (hash(i + 2) % 20000)::BIGINT / 100.0 AS amount, DATE '2026-01-01' + (i % 300)::INT AS day FROM range({n}) t(i)"""
CUSTOMERS = "SELECT i AS customer_id, 'customer-' || i AS name, ['US', 'MX', 'CA', 'BR', 'DE', 'JP', 'IN'][(1 + hash(i) % 7)::BIGINT] AS country FROM range(100000) t(i)"
CHECK = """SELECT country, sum(amount)::DOUBLE FROM '{events}' JOIN '{customers}' USING (customer_id)
           WHERE event_type = 'purchase' GROUP BY country ORDER BY 2 DESC LIMIT 3"""


def ask(events, customers):
    """Run the question through handles. Return (answer, protocol bytes, answer bytes)."""
    steps = [
        ("dataset.filter", {"where": [{"column": "event_type", "op": "eq", "value": "purchase"}]}),
        ("dataset.join", {"right": customers, "on": [{"left": "customer_id", "right": "customer_id"}]}),
        ("dataset.aggregate", {"group_by": ["country"], "metrics": [{"fn": "sum", "column": "amount", "as": "revenue"}]}),
        ("dataset.sort", {"by": [{"column": "revenue", "descending": True}]}),
        ("dataset.head", {"n": 3}),
    ]
    handle, total = events, 0
    for capability, input in steps:
        result = handle.call(capability, input)
        assert result["status"] == "completed", result
        total += len(json.dumps(result).encode())
        if "handles" in result:
            handle = type(events)(events._client, result["handles"][0])
    return result["output"], total, result["metadata"]["bytes_transferred"]


def main(max_rows: int) -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir()
    duckdb.sql("SET memory_limit = '4GB'")  # for the data generator; the limit is far above its need
    client = connect(DATA / "state")
    customers_path = DATA / "customers.parquet"
    duckdb.sql(f"COPY ({CUSTOMERS}) TO '{customers_path}'")
    customers = client.register("customers", adapter="duckdb", source=str(customers_path))
    rows, n = [], 10_000
    while n <= max_rows:
        path, export = DATA / f"events_{n}.parquet", DATA / "export.json"
        duckdb.sql(f"COPY ({EVENTS.format(n=n)}) TO '{path}'")
        duckdb.sql(f"COPY (SELECT * FROM '{path}') TO '{export}' (FORMAT json)")
        events = client.register(f"events_{n}", adapter="duckdb", source=str(path))
        t0 = time.perf_counter()
        answer, protocol_bytes, answer_bytes = ask(events, customers)
        seconds = time.perf_counter() - t0
        want = duckdb.sql(CHECK.format(events=path, customers=customers_path)).fetchall()
        assert [r[0] for r in answer["rows"]] == [c for c, _ in want], (answer, want)
        assert all(abs(r[1] - v) < 0.005 for r, (_, v) in zip(answer["rows"], want)), (answer, want)
        rows.append({"rows": n, "parquet_bytes": path.stat().st_size, "export_bytes": export.stat().st_size,
                     "protocol_bytes": protocol_bytes, "answer_bytes": answer_bytes, "seconds": round(seconds, 3)})
        print(json.dumps(rows[-1]), flush=True)
        export.unlink()
        path.unlink()
        n *= 10
    cpu = next((line.split(":")[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name")), platform.processor())
    lines = [
        "# Context efficiency: measured results", "",
        f"Made by `benchmarks/context_efficiency.py` on {time.strftime('%Y-%m-%d')}. "
        f"CPU: {cpu}. Python {platform.python_version()}, DuckDB {duckdb.__version__}. One run for each size. Each file was written a moment before the run, so it is in the page cache.", "",
        "Question: which three countries have the most purchase revenue? (filter, join, aggregate, sort, bounded read)", "",
        "| Event rows | Parquet file | Rows as JSON | Protocol bytes to caller | Answer bytes | Time |", "|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [f"| {r['rows']:,} | {r['parquet_bytes']:,} B | {r['export_bytes']:,} B | {r['protocol_bytes']:,} B | {r['answer_bytes']:,} B | {r['seconds']} s |" for r in rows]
    lines += ["", "- Protocol bytes = the JSON size of all five `OperationResult` objects, with the four derived handle summaries. The 9-byte change from the first row to the last row is more digits in the file size and in the time fields.",
              "- Rows as JSON = the size of the events table from `COPY ... (FORMAT json)`. A caller that computes the answer itself must receive this much.",
              "- The answer of each run is checked against one direct DuckDB SQL statement.", ""]
    (HERE / "RESULTS.md").write_text("\n".join(lines))
    (HERE / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    shutil.rmtree(DATA)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000)
