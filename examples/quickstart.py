"""Ask one question of two files through handles. Run: .venv/bin/python examples/quickstart.py"""
import tempfile
from pathlib import Path

import duckdb

from datahandles import Client, HandleError, connect

work = Path(tempfile.mkdtemp())
duckdb.sql(f"""COPY (SELECT i AS id, i % 500 AS customer_id, ['purchase', 'view'][1 + i % 2] AS event_type,
               (i % 97) * 1.5 AS amount FROM range(1000000) t(i)) TO '{work}/events.parquet'""")
duckdb.sql(f"COPY (SELECT i AS customer_id, ['US', 'MX', 'CA'][1 + i % 3] AS country FROM range(500) t(i)) TO '{work}/customers.csv'")

client = connect(work / "state", principal="analyst")
events = client.register("events", adapter="duckdb", source=f"{work}/events.parquet")
customers = client.register("customers", adapter="duckdb", source=f"{work}/customers.csv")
print(events)                      # a reference: an id, a type, a schema. No rows.
print(events.discover())           # what this principal can do with it

# Each step returns a new handle. No step runs a query.
purchases = events.invoke("dataset.filter", {"where": [{"column": "event_type", "op": "eq", "value": "purchase"}]})
joined = purchases.invoke("dataset.join", {"right": customers, "on": [{"left": "customer_id", "right": "customer_id"}]})
revenue = joined.invoke("dataset.aggregate", {"group_by": ["country"], "metrics": [{"fn": "sum", "column": "amount", "as": "revenue"}]})
ranked = revenue.invoke("dataset.sort", {"by": [{"column": "revenue", "descending": True}]})

# A capability that returns a value runs the plan in DuckDB. Only the bounded answer comes back.
print(ranked.invoke("dataset.head", {"n": 3}))

# A second principal gets only what the owner grants. A derived handle gives no more than its inputs.
ranked.grant("agent", ["dataset.count"])
agent = Client(client.runtime, "agent").resolve(ranked.id)
print(agent.discover(), agent.invoke("dataset.count"))
try:
    agent.invoke("dataset.head")
except HandleError as e:
    print(e.code)
