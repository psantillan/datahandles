# Roadmap

The v0.1 order follows `ARCHITECTURE.md` section 18. A row is "built" only when a test covers it and
the mutation check passes.

| Step | State | Evidence |
|---|---|---|
| 1. Protocol specification and schemas | Built (0.1) | `SPECIFICATION.md`, `spec/protocol/` |
| 2. Local runtime: memory and filesystem adapters | Built | `tests/integration/test_acceptance.py` |
| 3. Composition: derived handles, graph export and submit | Built | same |
| 4. DuckDB adapter (Parquet, CSV, DuckDB files) | Built | same, and `benchmarks/RESULTS.md` |
| 5. Remote execution: HTTP worker, placement, async jobs | Built | `tests/unit/test_remote_worker.py`, `tests/unit/test_jobs.py`, the parity test in the acceptance file |
| 6. MCP adapter, HTTP transport, SDK ergonomics | Built | `tests/unit/test_mcp_adapter.py`, `tests/unit/test_http_transport.py`, `examples/remote.py` |
| 7. Conformance suite that a second implementation can run | Built | `tests/conformance/` uses protocol methods only. It ran against the reference server and worker as separate processes: 12 passed. No second implementation exists. |

This is the v0.1 list of `ARCHITECTURE.md` section 14.

## Acceptance table (`ARCHITECTURE.md` section 16)

| Row | State |
|---|---|
| External resource | Pass |
| Capability discovery | Pass |
| Bounded computation | Pass |
| Composition | Pass |
| Graph execution | Pass |
| Local/remote parity | Pass: one DuckDB question gives equal outputs on a local handle and on a worker handle, and no handle shows its place |
| Resource independence | Pass |
| Context efficiency | Pass, measured in `benchmarks/RESULTS.md` |
| Persistence | Pass for handles, grants, stored results, jobs, registered workers, and the operation log |
| Security | Pass for grants, inheritance, input validation, path containment, bearer tokens on the HTTP transport, and signed tasks to a worker. No TLS in the runtime. |
| Temporal correctness | Pass for `latest`, `snapshot`, and `continuous` (a stream, and recorded windows that never change) |
| Failure recovery | Pass: a failed operation is recorded and the runtime continues, a lost worker gives a typed error that a caller can retry, and a job that a restart interrupts runs again only if its capability is idempotent |

## v0.2: Any resource (built)

Goal: show that a resource does not have to be a dataset. One handle, capability, grant, and job
model must serve a git repository and a browser session with no special code for either kind.

This order differs from `ARCHITECTURE.md` section 14. There, v0.2 is distributed and temporal
execution, and stateful adapters come in v0.3. A PostgreSQL adapter would show again that the
model works for data, and v0.1 shows that already. So the resource-independence work comes first.

| Step | State | Evidence |
|---|---|---|
| 1. A lock for each resource, not for each adapter | Built | `tests/unit/test_concurrency.py`: a long call on one resource does not delay a call on a second resource; calls on one resource still take turns |
| 2. More than one job thread | Built | same file: two long jobs run at the same time |
| 3. A cancel hook in the adapter interface | Built | same file: `job.cancel` stops the adapter call, in process and on a worker |
| 4. `limits.timeout_ms` | Built | same file: a call past its limit ends with `OPERATION_TIMEOUT` and its resource is free again |
| 5. Adapters outside the package | Built | `tests/unit/test_plugins.py`: an adapter from a separate installed package runs in the runtime and on a worker |
| 6. Git adapter | Built | `tests/unit/test_git_adapter.py` |
| 7. Browser-session adapter | Built | `tests/unit/test_browser_adapter.py`: cancel and a time limit stop a page load that never answers |
| 8. Acceptance: one agent task on two kinds of resource | Built | `benchmarks/AGENT_MCP_RESULTS.md`: a 9B model, through the MCP server, 5 of 5 correct; `tests/integration/test_two_resource_task.py` makes the same calls |

Steps 3 and 4 changed the adapter interface: `invoke` takes a `call`, and an adapter can declare `lock_key` (`SPECIFICATION.md` section 18).

## v0.3: Replayable graphs (built)

Goal: a workflow is a graph of operations with a handle at each step. A caller can change one
part and run it again, and only that part and the steps after it run. The same model must hold
for long simulations, sensor streams, and actions in the physical world.

| Step | State | Evidence |
|---|---|---|
| 1. A stable identity for each graph node, and reuse of unchanged results | Built | `tests/unit/test_graphs.py`: change one node of a five-step graph, and only that node and the one after it run (two operation log rows); the output equals a full run |
| 2. Job nodes in a graph | Built | same file: a graph registers as a handle; `graph.run` as a job returns at once and a cancel reaches the long node; after a restart the job goes on and the finished node does not run again |
| 3. Rules for actions that change the world | Built | same file: an action waits until approved, one approval allows one run, a finished action is reused, an edited action needs a new approval, and a principal that may only run the graph cannot approve |
| 4. Recorded windows | Built | `tests/unit/test_room_adapter.py`: a graph over a window gives equal outputs on each replay while the room goes on |
| 5. Streams and subscriptions | Built | same file: a subscriber gets each new reading in order, in process and over HTTP (server-sent events) |
| 6. A simulation adapter with fork | Built | `tests/unit/test_sim_adapter.py` |
| 7. A brief form of `handle.describe` | Built; target partly met | `benchmarks/DESCRIBE_COMPARISON.md`: the peak prompt is lower in 11 of 12 dataset cells and in each two-resource run; two-resource 5 of 5 as before; dataset 11 of 12 against 12 of 12 (one run for each cell; the wrong run joined the unfiltered table) |
| 8. A cancel hook for the DuckDB adapter (`DuckDBPyConnection.interrupt`) | Built | `tests/unit/test_duckdb_cancel.py`: a cancel ends a join of 6.4 billion rows in under 3 s |
| 9. Acceptance: one agent edits and runs a graph over a sensor, a simulation, and an action | Built | `benchmarks/AGENT_V03_RESULTS.md`: a 9B model through the MCP server, 5 of 5 runs pass all seven checks; `tests/integration/test_v03_acceptance.py` makes the same calls |

Steps 1 to 3 change only the runtime and the graph schema. Step 7 changes the protocol.

## v0.3.1: An HTTP API adapter (built)

| Item | Evidence |
|---|---|
| A generic HTTP JSON API adapter (`api`): live `api.get`, stored responses that never change (`api.fetch`, `response.*`), `api.post` as an action | `tests/unit/test_api_adapter.py` against a local service; `examples/public_apis.py` against Open-Meteo, USGS, and GitHub |
| The MCP server registers an API from one string (`--register weather=api:https://api.open-meteo.com/v1`) | same file: a source given as one string is the base URL |

## After v0.3.1 (built, not released)

| Item | Evidence |
|---|---|
| An API key in a named header (`token_header`) or in a query parameter (`token_query`) | `tests/unit/test_api_adapter.py`; `examples/public_apis.py` against the NASA API with its public demo key |
| A token value that cannot go in a request gives an error without the value | same file. Before, the error message of the Python HTTP library carried the value to the caller. |

## v0.4 and later

- Worker discovery, heartbeats, leases, and scheduling between workers. One handle is on one worker.
- Checkpoint and resume inside one job.
- A PostgreSQL adapter.
- `execution.priority`, `execution.deadline`, `lifecycle.expires_at`.
- Agent-as-resource, capability delegation, cross-runtime references, a TypeScript SDK
  (`ARCHITECTURE.md` section 14, v0.3).
- A second implementation of the protocol.

## Known limits of the 0.3 code

Transport and workers:

- The HTTP transport and the worker use plain HTTP from the Python standard library. A bearer
  token and a task signature give identity and integrity. They give no secrecy. Put both behind
  TLS or on a private network.
- The runtime keeps one HTTP request open to the worker for the full time of an operation.
- A cancel to a worker is one more HTTP request with no socket timeout. If the worker is not
  reachable, `job.cancel` waits until the connection fails.
- A task to a worker is valid for 30 seconds after the signature. This number is assumed, not
  measured. The clocks of the two machines must agree within that time.
- The registry file holds the key of each worker.
- Objects of the `memory` adapter on a worker stay in the worker process when the runtime restarts.
- The in-process Python API trusts the principal name that the caller gives. Only the HTTP
  transport authenticates.

Jobs:

- Jobs run on one thread for each CPU by default (`job_threads`), and start oldest first.
- The DuckDB adapter keeps one lock for the whole adapter (one shared connection), so its long
  jobs still run one at a time.
- A cancel and a time limit are cooperative. An adapter that does not honour them keeps running
  and keeps its lock until it returns.
- The idempotency key applies to jobs only. A synchronous operation that a caller sends two times
  runs two times.

Graphs:

- Reuse trusts the version that an adapter gives. An adapter that changes state without a new
  version would give a stale result.
- An idempotent write inside a graph (for example `object.set`) changes the version of its target,
  so the next run of the graph runs the nodes after it again.
- An approval belongs to the structural key of an action, not to one graph. An approval in one
  graph also allows the same action (same handles, same input) in another graph, one time.
- An approved action runs with the grants of the principal that runs the graph. The approver does
  not lend its grants.
- An action that fails after it started uses its approval. A new approval is needed for a new try.
- The output of `graph.run` has the default size limit (65,536 bytes). A graph with many large
  value nodes fails with `RESULT_TOO_LARGE`.
- A stream runs only on a handle in the runtime process, not on a remote worker.
- The room adapter is a simulation. Its physics numbers are assumed, not measured.

Data:

- The version of a file-backed dataset is its modification time and size. A `latest` handle reads
  the file again on each value request. A change during one query is DuckDB's to handle.
- The version of a directory handle changes when an entry is added or removed, not when a file
  in it is edited.
- `dataset.join` needs both handles on the DuckDB adapter in the same place. There is no
  cross-adapter join and no cross-worker join.
- The default size limit of one value output is 65,536 bytes. This number is assumed, not measured.

Agents:

- The brief `handle_describe` makes a model guess input shapes. The schema comes with the first
  input error, so a model pays for a schema only when it guesses wrong. On the dataset benchmark
  this gave 11 of 12 against 12 of 12 with the full describe (`benchmarks/DESCRIBE_COMPARISON.md`).
- A handle id is 26 opaque characters. In one of 12 agent runs the model dropped a character when
  it copied an id (`benchmarks/AGENT_RESULTS.md`). The `HANDLE_NOT_FOUND` error now offers the near
  id (`details.did_you_mean`). The runtime has no short alias for a handle. The search for a
  near id reads every live handle id, on the error path only.
- The dataset benchmark ran before the MCP adapter existed. It used the same two tool definitions
  in process. The two-resource task (`benchmarks/AGENT_MCP_RESULTS.md`) ran through the MCP server.
- The two-resource task and the v0.3 task are each one task with one model, 5 runs.

APIs:

- The api adapter does GET and POST only. It has no PUT, PATCH, or DELETE.
- A credential is one fixed value from an environment variable, in one header or one query
  parameter. There is no OAuth flow, no token refresh, and no request signing.
- An api handle with no `timeout_ms` has no time limit. A request waits as long as the service
  keeps the connection open, or until a cancel.
- Requests to one API run one at a time (one lock for each base URL).
- There is no retry, no rate limit, and no paging. A 429 status comes back to the caller.
- A stored response keeps JSON, or text read as UTF-8. A binary body is not kept exactly.
- The largest stored response is 10 MiB by default. This number is assumed, not measured.
- The tests use a local service. Only `examples/public_apis.py` calls real APIs, and CI does not run it.
  The NASA demo key has a low rate limit: its `X-RateLimit-Limit` header gave 10 on 2026-11-14.

Browser:

- Inside one action, Playwright waits up to 30 seconds (its own default) for an element or a page
  load. A session opened with `timeout_ms` ends the action earlier.
- A session reaches every http and https address that the process can reach (`SECURITY.md`).

Install:

- The git adapter needs git 2.24 or newer (`--end-of-options`). The tests ran on git 2.43.
- The JSON Schemas load from `spec/protocol/` in the repository tree. Use a checkout with an
  editable install (`pip install -e .`). A wheel does not carry the schemas.
