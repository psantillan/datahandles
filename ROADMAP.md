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

## v0.2: Any resource

Goal: show that a resource does not have to be a dataset. One handle, capability, grant, and job
model must serve a git repository and a browser session with no special code for either kind.

This order differs from `ARCHITECTURE.md` section 14. There, v0.2 is distributed and temporal
execution, and stateful adapters come in v0.3. A PostgreSQL adapter would show again that the
model works for data, and v0.1 shows that already. So the resource-independence work comes first.

| Step | State | Done when |
|---|---|---|
| 1. A lock for each resource, not for each adapter | Not built | A long call on one resource does not delay a call on a second resource of the same adapter |
| 2. More than one job thread | Not built | Two long jobs run at the same time |
| 3. A cancel hook in the adapter interface | Not built | `job.cancel` on a running job stops the adapter call |
| 4. `limits.timeout_ms` | Not built | An adapter call that does not end in time ends with a typed error and releases its lock |
| 5. Adapters outside the package | Not built | An adapter from a separate installed package registers and runs with no change to `datahandles` |
| 6. Git adapter | Not built | Log, diff, and file reads of a repository through handles, grants, and derived handles |
| 7. Browser-session adapter | Not built | A session handle with long, non-idempotent jobs. Cancel and timeout work on them |
| 8. Acceptance: one agent task on two kinds of resource | Not built | An agent completes one task through the MCP server with a git handle and a browser handle |

Steps 1 and 2 change only the runtime. Steps 3 and 4 change the adapter interface.

## v0.3 and later

- Worker discovery, heartbeats, leases, and scheduling between workers. One handle is on one worker.
- An event stream (SSE), `continuous` consistency, and subscriptions. A sensor-stream adapter needs these.
- Checkpoint and resume of a job.
- A PostgreSQL adapter.
- `execution.priority`, `execution.deadline`, `lifecycle.expires_at`.
- Agent-as-resource, capability delegation, cross-runtime references, a TypeScript SDK
  (`ARCHITECTURE.md` section 14, v0.3).
- A second implementation of the protocol.
- A brief form of `handle.describe` (see the limits below).

## Known limits of the 0.1 code

Transport and workers:

- The HTTP transport and the worker use plain HTTP from the Python standard library. A bearer
  token and a task signature give identity and integrity. They give no secrecy. Put both behind
  TLS or on a private network.
- The runtime keeps one HTTP request open to the worker for the full time of an operation.
- A task to a worker is valid for 30 seconds after the signature. This number is assumed, not
  measured. The clocks of the two machines must agree within that time.
- The registry file holds the key of each worker.
- Objects of the `memory` adapter on a worker stay in the worker process when the runtime restarts.
- The in-process Python API trusts the principal name that the caller gives. Only the HTTP
  transport authenticates.

Jobs:

- One thread runs the jobs, one at a time, oldest first.
- Each local adapter does one `invoke` at a time. A long job delays the other operations on the
  same adapter. Discovery and job submission do not wait.
- `job.cancel` on a running job discards the output. It does not stop the adapter call.
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
- The agent benchmark ran before the MCP adapter existed. It used the same two tool definitions
  in process. No model run through the MCP server is recorded.

Install:

- The JSON Schemas load from `spec/protocol/` in the repository tree. Use a checkout with an
  editable install (`pip install -e .`). A wheel does not carry the schemas.
