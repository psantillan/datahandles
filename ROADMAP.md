# Roadmap

The order follows `ARCHITECTURE.md` section 18. A row is "built" only when an acceptance test
covers it and the mutation check passes.

| Step | State | Evidence |
|---|---|---|
| 1. Protocol specification and schemas | Built (draft 0.1) | `SPECIFICATION.md`, `spec/protocol/` |
| 2. Local runtime: memory and filesystem adapters | Built | `tests/conformance/test_acceptance.py` |
| 3. Composition: derived handles, graph export and submit | Built | same |
| 4. DuckDB adapter (Parquet, CSV, DuckDB files) | Built | same, and `benchmarks/RESULTS.md` |
| 5. Remote execution: HTTP worker, dispatch, async jobs | Not built | the parity test is skipped |
| 6. MCP adapter, HTTP transport, SDK ergonomics | Not built | |
| 7. Conformance suite that a second implementation can run | Not built | the tests call the Python API directly |

## Acceptance table (`ARCHITECTURE.md` section 16)

| Row | State |
|---|---|
| External resource | Pass |
| Capability discovery | Pass |
| Bounded computation | Pass |
| Composition | Pass |
| Graph execution | Pass |
| Local/remote parity | Not built (step 5) |
| Resource independence | Pass |
| Context efficiency | Pass, measured in `benchmarks/RESULTS.md` |
| Persistence | Pass for handles, grants, stored results, and the operation log |
| Security | Pass for grants, inheritance, input validation, and path containment. No transport authentication yet. |
| Temporal correctness | Pass for `latest` and `snapshot`. `continuous` is not built. |
| Failure recovery | Part: a failed operation is recorded and the runtime continues. No async jobs, so no interrupted-job recovery yet. |

## Known limits of the 0.1 code

- All operations are synchronous and run in the runtime process.
- The in-process API trusts the principal name that the caller gives.
- The version of a file-backed dataset is its modification time and size. A `latest` handle reads
  the file again on each value request. A change during one query is DuckDB's to handle.
- The version of a directory handle changes when an entry is added or removed, not when a file
  in it is edited.
- `dataset.join` needs both handles on the DuckDB adapter. There is no cross-adapter join.
- The default size limit of one value output is 65,536 bytes. This number is assumed, not measured.
- A `handle_describe` result for a dataset costs a 9B model about 1,230 prompt tokens, because it
  carries the full input schema of nine capabilities. A brief form (names and one line each, the
  schema of one capability on request) is not built.
- A handle id is 26 opaque characters. In one of 12 agent runs the model dropped a character when
  it copied an id (`benchmarks/AGENT_RESULTS.md`). The `HANDLE_NOT_FOUND` error now offers the near
  id (`details.did_you_mean`). The runtime has no short alias for a handle. The search for a
  near id reads every live handle id, on the error path only.
