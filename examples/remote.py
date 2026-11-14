"""Ask a question over HTTP of a file that a remote worker holds. Run: .venv/bin/python examples/remote.py

This one program starts a worker, a runtime with the HTTP transport, and a client. In real use
they are three processes on up to three machines:

  python -m datahandles.workers.remote --state DIR --key-file FILE --adapters duckdb --port 8471
  python -m datahandles.protocols.http --state DIR --tokens FILE --port 8470 --worker w1,http://HOST:8471,FILE
"""
import tempfile
import threading
from pathlib import Path

import duckdb

from datahandles import Runtime, connect
from datahandles.protocols.http import Server
from datahandles.workers.remote import Worker

work = Path(tempfile.mkdtemp())
duckdb.sql(f"""COPY (SELECT i AS id, ['purchase', 'view'][1 + i % 2] AS event_type, (i % 97) * 1.5 AS amount
               FROM range(1000000) t(i)) TO '{work}/events.parquet'""")


servers = []


def start(server) -> str:
    servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


# The worker: a process next to the data. It holds a key, not a list of principals.
worker_url = start(Worker(work / "worker", "a shared key", ["duckdb"]).serve())

# The runtime: it holds the registry and the grants, and it does each check before it sends a task.
runtime = Runtime(work / "state")
runtime.add_worker("w1", worker_url, "a shared key")
runtime_url = start(Server(runtime, {"token-analyst": "analyst"}, creators={"analyst"}).serve())

# The client: it holds a URL and a token. The token says who it is.
client = connect(runtime_url, token="token-analyst")
events = client.register("events", adapter="duckdb", source=f"{work}/events.parquet", worker="w1")
print(events)  # nothing in the handle says where the file is

# The short call form: events.filter(...) is events.invoke("dataset.filter", {...}).
purchases = events.filter(where=[{"column": "event_type", "op": "eq", "value": "purchase"}])
print(purchases.count())  # the worker runs the plan, and one number comes back

# A background operation gives a job handle at once. The job is a resource with its own capabilities.
job = purchases.invoke("dataset.aggregate", {"metrics": [{"fn": "sum", "column": "amount", "as": "revenue"}]}, eager=True, background=True)
print(job, job.invoke("job.status")["state"])
total = job.wait(timeout=60)  # the result is a handle to a stored output on the worker
print(total.head())

for server in servers:
    server.shutdown()
