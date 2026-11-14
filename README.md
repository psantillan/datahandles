# DataHandles

A protocol and a reference runtime that let an AI agent work on a resource through a reference,
with no copy of the resource in its context.

An agent gets a **handle**: an opaque id, a type, a schema, and a list of typed capabilities.
It invokes capabilities on the handle. A capability returns a new handle (no data moves) or a
bounded value (a small, size-limited answer). The computation runs where the resource is.

The property that this project tests:

> Context consumption should scale with the complexity of the question, not the size of the
> underlying dataset.

## Measured

One question (filter, join, aggregate, sort, bounded read) on a Parquet file that grows from
10,000 rows to 100,000,000 rows. Full method and all rows: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

| Event rows | Rows as JSON | Protocol bytes to caller | Answer bytes | Time |
|---:|---:|---:|---:|---:|
| 10,000 | 877,653 B | 3,730 B | 147 B | 0.073 s |
| 1,000,000 | 89,794,093 B | 3,732 B | 146 B | 0.106 s |
| 100,000,000 | 9,179,443,674 B | 3,739 B | 146 B | 1.681 s |

The dataset grows 10,000 times. The bytes that go to the caller grow by 9.

"Protocol bytes" is the JSON size of everything that the runtime returns for the question: five
operation results and four handle summaries. One machine, one run for each size, DuckDB 2.0.0.

## Example

```python
from datahandles import connect

client = connect(".datahandles", principal="analyst")
events = client.register("events", adapter="duckdb", source="events.parquet")
customers = client.register("customers", adapter="duckdb", source="customers.csv")

# Each step returns a new handle. No step runs a query.
purchases = events.invoke("dataset.filter", {"where": [{"column": "event_type", "op": "eq", "value": "purchase"}]})
joined = purchases.invoke("dataset.join", {"right": customers, "on": [{"left": "customer_id", "right": "customer_id"}]})
revenue = joined.invoke("dataset.aggregate", {"group_by": ["country"], "metrics": [{"fn": "sum", "column": "amount", "as": "revenue"}]})
ranked = revenue.invoke("dataset.sort", {"by": [{"column": "revenue", "descending": True}]})

# A capability that returns a value runs the plan in DuckDB. Only the bounded answer comes back.
ranked.invoke("dataset.head", {"n": 3})
# {'columns': ['country', 'revenue'], 'rows': [['US', 12095631.0], ['CA', 11951866.5], ['MX', 11951820.0]], 'truncated': False}

# A second principal gets only what the owner grants.
ranked.grant("agent", ["dataset.count"])
```

The full program is [`examples/quickstart.py`](examples/quickstart.py). It makes its own data.

## How it works

- **Handle.** A reference with a kind: `resource` (an external thing), `computation` (a plan
  that did not run), or `result` (a stored output). A handle holds no data and no credential.
- **Capability.** A typed operation with JSON Schemas for its input and output. It declares if
  it returns a value or a handle, its effects, and its output size limit.
- **Lazy by default.** An operation that returns a handle only records a plan step. The DuckDB
  adapter compiles the full plan to one SQL statement when a value is requested. `eager=True`
  runs the plan and stores the output as a `result` handle pinned to the source version.
- **Bounded output.** Each value capability has a byte limit. The runtime refuses a larger
  output with a typed error. Adapters truncate and say so.
- **Authorization is separate from the handle.** A handle id gives no authority. The runtime
  checks each operation against the grants of the principal. A derived handle gives no more
  than the handles it derives from.
- **Adapters.** A resource type is one class with five methods: `describe`, `discover`,
  `invoke`, `observe`, `release`. The core does not know any resource type. Three adapters
  exist: `memory` (JSON objects), `filesystem` (read-only directory tree), `duckdb` (Parquet,
  CSV, and DuckDB files).
- **Registry.** Handles, grants, lineage, and an operation log live in SQLite and survive a
  restart.
- **No language model is needed.** The runtime is a plain library.

## State

Version 0.1, in development. Built: the protocol schemas, the local runtime, three adapters,
composition and graphs, the registry, and a Python client. Not built yet: remote workers, async
jobs, HTTP and MCP transports. [`ROADMAP.md`](ROADMAP.md) has the full table, with the limits of
the current code.

## Documents

| File | Content |
|---|---|
| [`SPECIFICATION.md`](SPECIFICATION.md) | The protocol, in normative terms |
| [`spec/protocol/`](spec/protocol/) | JSON Schemas for handles, capabilities, operations, results, graphs, errors |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The design document that the project started from |
| [`ROADMAP.md`](ROADMAP.md) | What is built, what is not, known limits |
| [`SECURITY.md`](SECURITY.md) | What the runtime protects and what it does not |

## Run it

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python examples/quickstart.py
.venv/bin/python -m pytest tests/conformance/test_acceptance.py -q
.venv/bin/python tests/mutation_check.py
.venv/bin/python benchmarks/context_efficiency.py 10000000
```

- The acceptance tests have one test for each row of the acceptance table in the design document.
- The mutation check removes 20 guards from the source, one at a time, and confirms that a test
  fails for each.
- The benchmark argument is the largest row count. `100000000` needs about 11 GB of free disk.

## Layout

```
spec/protocol/        JSON Schemas (the protocol)
packages/core/        ids, errors, schema validation, graph ordering
packages/runtime/     runtime, registry, adapter contract
packages/adapters/    memory, filesystem, duckdb
packages/sdk/         Python client
tests/conformance/    acceptance tests
benchmarks/           context-efficiency benchmark and results
```

Python imports `packages/` as `datahandles`.

## License

MIT. See [`LICENSE`](LICENSE).
