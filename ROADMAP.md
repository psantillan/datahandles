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
| Temporal correctness | Pass for `latest` and `snapshot`. `continuous` is not built. |
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

## v0.3: Replayable graphs

Goal: a workflow is a graph of operations with a handle at each step. A caller can change one
part and run it again, and only that part and the steps after it run. The same model must hold
for long simulations, sensor streams, and actions in the physical world.

| Step | State | Done when |
|---|---|---|
| 1. A stable identity for each graph node, and reuse of unchanged results | Not built | Change one node of a five-step graph and submit it again: only that node and the nodes after it run (the operation log shows it), and the outputs equal a full run |
| 2. Job nodes in a graph | Not built | A graph with a long node returns at once; a caller can observe and cancel it; after a restart the graph goes on without running a finished node again |
| 3. Rules for actions that change the world | Not built | A finished node with an external, non-idempotent effect never runs again when the graph is submitted again; such a node waits in the status `waiting` until a principal with the right approves it |
| 4. Recorded windows | Not built | A capability records the observations of a live source into a `result` handle; a graph over that handle gives equal outputs on each replay while the live source changes |
| 5. Streams and subscriptions | Not built | `continuous` consistency and an event stream; a sensor-stream adapter (simulated sensor) delivers each new reading to a subscriber, and step 4 records a window of it |
| 6. A simulation adapter with fork | Not built | Two forks of one state take different inputs and give different states; the parent state does not change; the same inputs on a new fork give an equal state |
| 7. A brief form of `handle.describe` | Not built | Both agent benchmarks run again with a lower peak prompt and the same accuracy (12 of 12, 5 of 5) |
| 8. A cancel hook for the DuckDB adapter (`DuckDBPyConnection.interrupt`) | Not built | A cancel stops a running query |
| 9. Acceptance: one agent edits and runs a graph over a sensor, a simulation, and an action | Not built | Through the MCP server, an agent records a sensor window, forks a simulation from it, changes one node, runs the graph again (only the changed part runs), and the action node waits for approval |

Steps 1 to 3 change only the runtime and the graph schema. Step 7 changes the protocol.

## v0.4 and later

- Worker discovery, heartbeats, leases, and scheduling between workers. One handle is on one worker.
- Checkpoint and resume inside one job.
- A PostgreSQL adapter.
- `execution.priority`, `execution.deadline`, `lifecycle.expires_at`.
- Agent-as-resource, capability delegation, cross-runtime references, a TypeScript SDK
  (`ARCHITECTURE.md` section 14, v0.3).
- A second implementation of the protocol.

## Known limits of the 0.2 code

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
  jobs still run one at a time. It has no cancel hook: a cancel or a time limit gives the answer
  at once, but the query runs to its end.
- A cancel and a time limit are cooperative. An adapter that does not honour them keeps running
  and keeps its lock until it returns.
- The idempotency key applies to jobs only. A synchronous operation that a caller sends two times
  runs two times.

Data:

- The version of a file-backed dataset is its modification time and size. A `latest` handle reads
  the file again on each value request. A change during one query is DuckDB's to handle.
- The version of a directory handle changes when an entry is added or removed, not when a file
  in it is edited.
- `dataset.join` needs both handles on the DuckDB adapter in the same place. There is no
  cross-adapter join and no cross-worker join.
- The default size limit of one value output is 65,536 bytes. This number is assumed, not measured.

Agents:

- A `handle_describe` result for a dataset costs a 9B model about 1,230 prompt tokens, because it
  carries the full input schema of nine capabilities. A brief form (names and one line each, the
  schema of one capability on request) is not built.
- A handle id is 26 opaque characters. In one of 12 agent runs the model dropped a character when
  it copied an id (`benchmarks/AGENT_RESULTS.md`). The `HANDLE_NOT_FOUND` error now offers the near
  id (`details.did_you_mean`). The runtime has no short alias for a handle. The search for a
  near id reads every live handle id, on the error path only.
- The dataset benchmark ran before the MCP adapter existed. It used the same two tool definitions
  in process. The two-resource task (`benchmarks/AGENT_MCP_RESULTS.md`) ran through the MCP server.
- The two-resource task is one task with one model, 5 runs.

Browser:

- Inside one action, Playwright waits up to 30 seconds (its own default) for an element or a page
  load. A session opened with `timeout_ms` ends the action earlier.
- A session reaches every http and https address that the process can reach (`SECURITY.md`).

Install:

- The git adapter needs git 2.24 or newer (`--end-of-options`). The tests ran on git 2.43.
- The JSON Schemas load from `spec/protocol/` in the repository tree. Use a checkout with an
  editable install (`pip install -e .`). A wheel does not carry the schemas.
