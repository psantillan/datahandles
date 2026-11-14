# DataHandles Protocol Specification

Version 0.3. The key words MUST, MUST NOT, SHOULD, and MAY have the meanings of RFC 2119.

This file and the JSON Schemas in `spec/protocol/` define the protocol. The Python code in
`packages/` is one implementation of it. Where the code and this file differ, this file governs
and the code has a defect. `ARCHITECTURE.md` is the design document that this file comes from.

## 1. Model

| Term | Meaning |
|---|---|
| Resource | Something that exists outside the caller: a file tree, a table, an object. |
| Handle | A reference to a resource or to a computation. It is not the resource. |
| Capability | One typed operation that a resource supports. |
| Operation | A request to invoke one capability on one handle. |
| Result | The record of one operation: a bounded value, or new handles, or an error. |
| Principal | The identity that makes a request. |
| Grant | A record that lets one principal invoke named capabilities on one handle. |
| Adapter | The code that connects one kind of resource to the runtime. |
| Runtime | The code that resolves handles, checks grants, and calls adapters. |

A caller never needs to hold a resource. It holds a handle, and it asks the runtime to compute.

## 2. Identity

- A handle id MUST have the form `hdl_` + a 26-character ULID. An operation id MUST have the form
  `op_` + a 26-character ULID.
- An id MUST be opaque. It MUST NOT contain a storage location, a principal, or a secret.
- Possession of an id gives no authority. See section 8.

## 3. Handle

Schema: `spec/protocol/handles.schema.json`.

- `kind` is one of:
  - `resource`: a reference to an external resource.
  - `computation`: a reference to a planned computation. The computation did not run yet.
  - `result`: a reference to the stored output of a computation.
  - `job`: a reference to an operation that runs asynchronously (section 14).
- `type` names the resource type (`dataset`, `filesystem`, `object`, `job`). It is the prefix of each
  capability name of the handle.
- `version` is the resource version that a `result` handle is pinned to, or that a `resource`
  handle had when it was registered. It is empty for a `computation` handle.
- `capabilities` lists the capabilities that the requesting principal holds on this handle. Two
  principals MAY get different lists for the same handle. The list is a description. It is not an
  authorization: the runtime checks each operation again (section 8).
- `parents` lists the handles that this handle derives from.
- A handle MUST NOT contain resource data, and MUST NOT contain a credential.

A `HandleSummary` (in `results.schema.json`) is a Handle in which each capability is given by name
only. An `OperationResult` carries summaries. `handle.describe` returns the full Handle.

## 4. Capability

Schema: `spec/protocol/capabilities.schema.json`.

- `name` MUST have the form `<resource type>.<verb>`, for example `dataset.filter`.
- `returns` is one of:
  - `value`: the operation sends a bounded observation to the caller.
  - `handle`: the operation derives a new handle. No resource data goes to the caller.
- A capability that returns a value MUST declare `limits.max_output_bytes`. The runtime MUST
  refuse to send a larger output (`RESULT_TOO_LARGE`). An adapter SHOULD truncate its output
  below the limit and say so in the output (`"truncated": true`).
- `effects` states if the capability reads, writes, or has an effect outside the resource.
- The protocol defines no common verb set. Each adapter defines the capabilities of its type.

## 5. Operation

Schema: `spec/protocol/operations.schema.json`.

- `input` MUST pass the `input_schema` of the capability.
- An input value of the form `{"$handle": "hdl_..."}` is a reference to a second input handle.
  The runtime resolves it and checks the grant of the principal on it (section 8). This is how one
  operation takes two or more handles, for example a join.
- `execution.strategy` applies to a capability that returns a handle:
  - `lazy` (default): the runtime validates the operation and records a `computation` handle.
    It does not run the computation.
  - `eager`: the runtime runs the computation now and records a `result` handle.
- `execution.async` `true` asks the runtime to run the operation as a job (section 14).
- `execution.mode` limits the place of the execution. It does not select the place: an operation
  runs where its target handle is (section 15).
  - `auto` (default): no limit.
  - `local`: the operation MUST run in the runtime process.
  - `remote`: the operation MUST run on a worker.
  - If the handle is not in the place that the mode names, the runtime MUST refuse the operation
    (`INVALID_INPUT`, with `details.placement`). It MUST NOT move the resource.

## 6. Result

Schema: `spec/protocol/results.schema.json`.

- `status` `completed`: the result has `output` (capability returns a value) or `handles`
  (capability returns a handle), not both.
- `status` `failed`: the result has `error` (`errors.schema.json`) and has no `output`.
- `status` `running`: the runtime accepted the operation as a job. The result has one handle of
  the kind `job` in `handles`, and has no `output`.
- `metadata.bytes_transferred` is the JSON size of `output`.
- `metadata.source_version` is the resource version that the operation read.
- A protocol failure is a result with `status` `failed`. It is not a transport failure.

## 7. Composition and graphs

Schema: `spec/protocol/graphs.schema.json`.

- A capability that returns a handle makes a handle that a later operation can take as its target
  or as a `$handle` input. A chain of such operations is a computation graph.
- The graph MUST be acyclic. A runtime MUST reject a graph with a cycle (`GRAPH_CYCLE`).
- A runtime MUST run the operations of a submitted graph in dependency order, whatever the order
  of the node list.
- An edge with no `input_name` connects a node to the target of an operation. An edge with
  `input_name` connects a node to that input field.
- A runtime MUST be able to export the graph that made a handle (lineage).
- When a value is requested from a `computation` handle, the adapter runs the full plan at the
  resource. Intermediate data does not go to the caller.

**Node identity and reuse.** A runtime that runs graphs SHOULD give each node a key and reuse
the result of a node that did not change:

- A resource node: its handle id and its version now (`resource.observe`).
- An operation node: its capability, its plain input, lazy or eager, and the keys of the nodes
  that it takes. For a capability with `execution.deterministic` `true`, the key of a resource
  node that it takes directly does not include the version.
- If the same principal ran a node with an equal key before, and the handle it made is still
  live, the stored result is the result again, with `metadata.reused` `true`. The operation does
  not run. So a caller can change one node and run the graph again: only that node and the nodes
  after it run.
- A node that fails or waits blocks the nodes after it. They do not run and have no result.
  Nodes that do not depend on it still run.

**Actions.** An action is a capability that writes (`effects.write`), reaches outside
(`effects.external`), and is not idempotent. In a graph:

- An action node has a structural key: handle ids, capabilities, and inputs, with no versions,
  because the action itself changes the version of its target.
- It MUST run only with an unused approval for its key. The run MUST use the approval when it
  starts, so an action that stops in the middle needs a new approval.
- With no approval: an action that ran before gives its stored result (`reused`), and an action
  that never ran gets the status `waiting`.
- An edit of the action, or of a node before it, gives a new key that needs its own approval.

**A graph as a handle.** A runtime MAY offer a graph as a resource (type `graph`) with these
capabilities: `graph.run` (run it and return the result of each node; with `execution.async`
it is a job), `graph.set` (derive a graph with new input for one node), and `graph.approve`
(record one approval for one node). `graph.approve` is a write, so a principal that may run a
graph does not also get the right to approve its actions.

## 8. Authorization

- A grant names a principal, a handle, and capability names (or `*` for all).
- The principal that registers a resource is its owner and holds `*` on it.
- A principal MAY grant to another principal only capabilities that it holds itself.
- On a derived handle, a principal holds: the grants made on that handle, plus the capabilities
  that it holds on **every** parent. A derivation therefore gives no more authority than its
  inputs.
- An operation with a `$handle` input needs the same capability on that input handle.
- A `job` handle is the exception to inheritance. It belongs to the principal that submitted the
  operation, and a principal gets nothing on it from a grant on its parents.
- For each operation the runtime MUST do these checks in this order:
  1. Take the authenticated principal from the transport.
  2. Resolve the handle. If the principal holds no capability on it, the answer MUST be
     `HANDLE_NOT_FOUND`, the same as for an id that does not exist.
  3. Check that the handle and its parents are not released (`HANDLE_RELEASED`).
  4. Check that the capability exists (`CAPABILITY_NOT_FOUND`) and that the principal holds it
     (`CAPABILITY_DENIED`).
  5. Validate the input (`INVALID_INPUT`).
  6. Resolve and check each `$handle` input.
  7. Check the limit of `execution.mode` against the place of the handle.
  8. Call the adapter, or put the operation in the job queue. If the capability declares
     `limits.timeout_ms`, the runtime MUST answer `OPERATION_TIMEOUT` at that time. The adapter
     call may continue after the answer (section 18 has how the reference runtime stops it).
  9. Record the operation: principal, target, capability, input, status, duration, bytes, version.
- An adapter never sees a principal or a grant. An adapter MUST keep each operation inside the
  resource of the handle (for example, a filesystem adapter MUST NOT follow a path or a symlink
  out of the root).
- A caller supplies names and values. An adapter MUST NOT run query text from a caller.

## 9. Consistency

| Value | Meaning |
|---|---|
| `latest` | Each operation reads the current state of the resource. |
| `snapshot` | The handle is pinned to one version. Later changes to the resource do not change its answers. |
| `continuous` | A live source whose readings come one after another, for example a sensor. A caller subscribes to it, or records a window of it. |

A `result` handle is a snapshot. A `computation` handle on a `latest` resource is `latest`: its
plan runs again on each request for a value.

## 10. Lifecycle

- `persistence` `persistent`: the handle MUST survive a runtime restart. `ephemeral`: the handle
  ends with the runtime process. A handle derived from an ephemeral handle is ephemeral.
- `handle.release` ends a handle. Each handle derived from it ends too. Only the owner can
  release. A release MUST NOT delete the external resource. It MAY delete a stored result that
  the runtime made.

## 11. Errors

Schema: `spec/protocol/errors.schema.json`. Each error has `code`, `message`, `retryable`, and
optional `details` for recovery (for example the capability names that the caller does hold).

| Code | Meaning |
|---|---|
| `HANDLE_NOT_FOUND` | No such handle, or the principal holds nothing on it. |
| `HANDLE_RELEASED` | The handle, or a handle it derives from, was released. |
| `CAPABILITY_NOT_FOUND` | The resource type has no such capability. |
| `CAPABILITY_DENIED` | The principal does not hold the capability, or the operation tries to leave the resource. |
| `INVALID_INPUT` | The operation or its input does not pass a schema, or names something that is not there. |
| `RESULT_TOO_LARGE` | The output is larger than `limits.max_output_bytes`. No output was sent. |
| `GRAPH_CYCLE` | The submitted graph has a cycle. |
| `ADAPTER_ERROR` | The adapter had a fault. The runtime continues. |
| `WORKER_UNAVAILABLE` | The worker of the handle gave no answer (`retryable` true), or it is not registered, or it refused the task. |
| `JOB_NOT_FINISHED` | `job.result` was called before the job ended. `retryable` is true, and `details.state` has the state. |
| `JOB_CANCELLED` | The job was cancelled. It has no result. |
| `JOB_INTERRUPTED` | The runtime stopped while the job ran, and the job cannot run again safely. |
| `OPERATION_TIMEOUT` | The call did not end within `limits.timeout_ms` of its capability. `details.timeout_ms` has the limit. The effects of the call are unknown. |

A `HANDLE_NOT_FOUND` error MAY carry `details.did_you_mean`: a list of handle ids that the caller
probably means (the given text contains the id, or differs from it by a small number of
characters). The list MUST contain only live handles that the principal holds a capability on.
The error for a handle that the principal cannot use MUST therefore stay the same as the error
for an id that does not exist.

## 12. Decisions that add to the design document

| Decision | Reason |
|---|---|
| `Handle.kind` (`resource`, `computation`, `result`) | The design document names three handle categories. A caller must be able to tell them apart. |
| `Capability.returns` (`value`, `handle`) | The runtime must know before dispatch if an operation sends data to the caller. |
| `{"$handle": id}` in operation input | One `target` field cannot express an operation on two handles. |
| `execution.strategy` on an operation | The design document puts lazy and eager on the graph only. A single operation needs the same choice. |
| `HandleSummary` in results | With full capability lists in each result, one five-operation question sent 23,094 bytes to the caller. With summaries it sends 3,729 bytes (`benchmarks/context_efficiency.py`, 10,000 rows). |
| `limits.max_output_bytes` required for value capabilities | "Bounded output" must be a property that the runtime enforces, not a habit of each adapter. |
| No-grant answer is `HANDLE_NOT_FOUND` | A principal must not learn which ids exist. |
| `Handle.parents` | Lineage and grant inheritance both need the derivation edges. |
| `details.did_you_mean` on `HANDLE_NOT_FOUND` | A 9B model dropped one character of a 26-character id and sent the wrong id three times before it recovered (`benchmarks/AGENT_RESULTS.md`). An opaque id gives a caller nothing to correct it from. |
| A job is a handle of the kind `job`, not a set of `job.*` protocol methods | The design document says "the job itself is a resource". As a handle, a job gets grants, discovery, lineage, release, and the tool surface for a model with no new mechanism. |
| The operation id is the idempotency key of a job | A caller that gets no answer to a submission must be able to send it again and not start the work twice. |
| All checks run again when a job starts | Time passes between submission and execution. A grant that the owner revoked in that time must stop the job. |
| `execution.mode` is a limit, not a selector | "Execution location must be an implementation detail." The place is a property of the handle. A caller that needs a guarantee about the place can state it, and gets an error if it does not hold. |
| No grant inheritance on a `job` handle | With inheritance, each principal with a grant on the target could read the result of another principal's job. |
| `handle.create` over HTTP needs a separate right | The method makes the runtime open a location that the caller names. A bearer token alone must not give that. |
| `limits.timeout_ms` is set by the capability, not by the caller | The adapter knows how long an action can take. An operation has no field for it, so a caller cannot remove a limit. |
| `OPERATION_TIMEOUT` is not `retryable` | A call that passed its limit once will probably pass it again. A write may have taken effect. |
| A cancel and a time limit are cooperative (section 18) | A runtime cannot stop a thread safely. The adapter knows how to stop its own work: kill a process, cancel a page load. |
| A graph node is reused by key, per principal | "Change one part and run it again" must not run the unchanged parts. A result belongs to the principal that made it, so reuse does too. |
| An action has a structural key and needs an approval for each run | A physical action must never run again by accident. A versioned key would see a new version after each run and run it again. |
| A failed or waiting node blocks only the nodes after it | A waiting action must not stop a simulation branch that does not depend on it. |
| `execution.deterministic` on a capability | A recorded window of a live source never changes, but the version of the source does. Without this flag, a graph would record the window again on each run. |
| Streams use `execution.mode` `stream` and `operation.subscribe` | The schema reserved the mode. A stream is many bounded values, so it is not one OperationResult. |
| `handle.describe` with `detail` `brief` and `capability` | One full description of a dataset handle cost a 9B model about 1,230 prompt tokens (the v0.2 run, `benchmarks/describe_comparison/dataset_full_describe.json`; `benchmarks/DESCRIBE_COMPARISON.md`). Most calls need the schema of one capability. |

## 13. Not in the reference runtime yet

The schemas reserve these. The reference runtime does not implement them. See `ROADMAP.md`.

- `execution.priority`, `execution.deadline`, `lifecycle.expires_at`.
- Worker heartbeats, leases, and scheduling between workers.
- A stream from a handle on a remote worker.

## 14. Jobs

An operation runs as a job when `execution.async` is `true`, or when its capability declares the
execution mode `async`.

- At submission the runtime MUST do checks 1 to 7 of section 8. If a check fails, the result is
  `failed` and no job exists.
- If the checks pass, the runtime stores the job and returns a result with the status `running`
  and one handle with `kind` `job` and `type` `job`. The `parents` of the job handle are the
  target and each `$handle` input of the operation.
- The submitter holds `*` on the job handle (section 8).
- A job has one of these states: `queued`, `running`, `completed`, `failed`, `cancelled`. The last
  three are final. A final state MUST NOT change.
- When the job starts, the runtime MUST do the checks of section 8 again.
- A runtime MAY run several jobs at the same time. Jobs start in the order of submission.
- **Idempotency.** The operation id is the key. If a principal submits an operation that is equal
  to one that it submitted before, the runtime MUST return the same job handle and MUST NOT start
  a second job. If the id is known but the operation differs, or the principal differs, the
  runtime MUST refuse it (`INVALID_INPUT`).

A job handle has three capabilities:

| Capability | Returns | Behavior |
|---|---|---|
| `job.status` | value | `state`, `attempts`, `operation_id`, `target`, `capability`, the times, and `error` for a failed job. |
| `job.cancel` | value | A queued job ends as `cancelled`. A running job gets a cancel signal (section 18); when the adapter returns, the runtime discards the output and the job ends as `cancelled`. A final state stays. The output is the status. |
| `job.result` | what the operation returns | For a completed job: the output, or the handle that the operation made, under the same size limit. For a failed job: the error of the operation. Before the end: `JOB_NOT_FINISHED`. After a cancel: `JOB_CANCELLED`. |

**Restart.** A job MUST survive a runtime restart. After a restart:

- A queued job runs.
- A job that was `running` runs again only if its capability declares `execution.idempotent`
  `true` and it has no cancel request. If not, it ends as `failed` with `JOB_INTERRUPTED`.
- An attempt that started before the restart MUST NOT change the job.

## 15. Placement and workers

- A runtime MAY keep a resource on a worker: a second process, usually on the machine that holds
  the resource. A handle is created on a worker, and each handle derived from it stays on that
  worker. The computation moves to the data.
- A Handle MUST NOT show its place. The same sequence of operations MUST give equal outputs on a
  local handle and on a remote handle of the same resource.
- All handles of one operation (the target and each `$handle` input) MUST be in the same place.
  If they are not, the runtime MUST refuse the operation (`INVALID_INPUT`).
- If the worker gives no answer, the operation fails with `WORKER_UNAVAILABLE` and `retryable`
  true. Handles in other places MUST continue to work.

The protocol between a runtime and its workers is not part of this specification, and a caller
never sees it. The reference runtime uses this one:

- `POST /v1/task` with a JSON task: `method` (`info`, `open`, `describe`, `discover`, `observe`,
  `release`, `invoke`, `cancel`), `adapter`, and for `invoke`: `ref`, `capability`, `input`, `refs`
  (the resolved `$handle` inputs, apart from the caller's values), `eager`.
- A `cancel` task names the id of a running `invoke` task and a reason (`cancel` or `timeout`). The
  worker cancels the adapter call of that task (section 18).
- The runtime does every check of section 8 before it sends a task. A task carries no principal.
- Each task has an `id`, an `expires` time (30 seconds after the signature; this number is
  ASSUMED), and the header `X-DataHandles-Signature`: HMAC-SHA256 of the body with a key that the
  runtime and the worker share. The worker refuses a task with a bad signature, a task past its
  expiry time, and a task id that it did before.
- A worker hosts only the adapters that its operator lists.

## 16. HTTP binding

A runtime that offers the protocol over HTTP MUST follow this binding.

- Request: `POST /v1/<method>`, with a JSON object of parameters as the body.
- Authentication: the header `Authorization: Bearer <token>`. The runtime takes the principal
  from the token. It MUST NOT take a principal from the request body. With no known token the
  answer is HTTP 401.
- Reply: HTTP 200 and `{"result": ...}`. A protocol error is HTTP 200 and `{"error": Error}`.
- A method that the runtime does not have gives HTTP 404. A parameter that the method does not
  have gives `INVALID_INPUT`.

| Method | Parameters | Result |
|---|---|---|
| `handle.create` | `name`, `adapter`, `source`, optional `worker` | Handle |
| `handle.resolve` | `name` | Handle |
| `handle.list` | | A list of Handles: each live handle that the principal holds a capability on |
| `handle.describe` | `handle`, optional `detail` (`full` or `brief`), optional `capability` | Handle. `brief`: each capability with its name, description, and returns, no schemas. `capability`: only that capability, in full. |
| `handle.release` | `handle` | null |
| `handle.grant` | `handle`, `to`, `capabilities` | null |
| `handle.revoke` | `handle`, `from` | null |
| `resource.observe` | `handle` | An object with `version` |
| `operation.invoke` | `operation` | OperationResult. A capability with `execution.mode` `stream` gives `INVALID_INPUT`. |
| `operation.subscribe` | `operation` | For a capability with `execution.mode` `stream`: `text/event-stream`, one `data:` line for each event, `{"result": {"output": ..., "version": ...}}`. An error after the start is the last event, `{"error": Error}`. An error before the start is a JSON reply as for other methods. The client closes the connection to stop. |
| `graph.export` | `handle` | Graph |
| `graph.submit` | `graph` | An object with `results` (operation node id to OperationResult) and `handles` (node id to handle id). A node that fails or waits blocks the nodes after it (section 7). |

- `handle.create` makes the runtime open a location that the caller names. A runtime MUST limit
  it to principals that the operator trusts, and it answers `CAPABILITY_DENIED` to each other
  principal.
- There are no job methods. A caller invokes `job.status`, `job.cancel`, and `job.result` on the
  job handle with `operation.invoke`.

## 17. Conformance

`tests/conformance/` is a test suite that uses only the methods of section 16. It runs against
any runtime that offers the HTTP binding:

```
DATAHANDLES_URL=http://127.0.0.1:8470 DATAHANDLES_TOKEN=... DATAHANDLES_TOKEN_2=... python -m pytest tests/conformance
```

- `DATAHANDLES_TOKEN` is the token of a principal that can call `handle.create`.
- `DATAHANDLES_TOKEN_2` is the token of a second principal, with the name `bob` or the name in
  `DATAHANDLES_PRINCIPAL_2`.

The suite needs one fixture resource type. A runtime under test MUST offer it:
`handle.create` with `adapter` `memory` and `source` `{"value": <JSON value>}` gives a handle of
the type `object` with these capabilities. `path` is a list of keys and list indexes.

| Capability | Input | Returns |
|---|---|---|
| `object.get` | optional `path` | value: the JSON value at the path |
| `object.keys` | optional `path` | value: `{"keys": [...]}` for a mapping, `{"length": n}` for a list |
| `object.select` | `path` | handle: the value at the path |
| `object.set` | `path`, `value` | value: `{"version": ...}`. The version of the resource changes. |
| `object.equals` | `other` (a `$handle` input) | value: `{"equal": true or false}` |
| `object.snapshot` | | handle: kind `result`, consistency `snapshot`, a copy at the current version |

The suite tests sections 2 to 11, 14, and 16. It does not test section 15 through HTTP, because
a caller cannot see the place of a handle.

The suite ran against the reference runtime only: in process, behind its HTTP server, and with
the fixture resources on a worker. No second implementation exists.

## 18. Adapters in the reference runtime

This section describes the reference runtime, not the protocol. A second implementation can
organize its adapters in another way.

An adapter is one class for one kind of resource. It never sees a principal, a grant, or a
handle id. It has six methods: `open` (a source to the reference of the root resource), `describe`,
`discover` (the capability descriptors), `invoke`, `observe` (a state summary with `version`),
and `release`.

**Locks.** `lock_key(ref)` says which calls must not run at the same time. Calls with different
keys run in parallel. The default (`None`) is one lock for the whole adapter: correct for an
adapter with one shared connection, such as DuckDB. The memory, filesystem, git, and browser
adapters return a key for each resource. `discover` never waits for a lock.

**Cancel and time limits.** `invoke` receives a `Call`. The runtime cancels it on `job.cancel` and
when `limits.timeout_ms` passes. A cancel is cooperative: the adapter registers a hook
(`call.on_cancel`) or checks `call.cancelled` between steps, then returns or raises. At the time
limit the runtime gives `OPERATION_TIMEOUT` at once. The adapter thread goes on until the adapter
honours the cancel, and the runtime discards its output. An adapter that ignores the cancel keeps
its lock until it returns.

**Installed adapters.** An adapter name is a built-in module (`datahandles.adapters.NAME`) or an
entry point in the group `datahandles.adapters` of an installed package. A built-in name wins.

| Adapter | Resource | Type | Lock | Cancel |
|---|---|---|---|---|
| `memory` | A JSON value in the runtime process | `object` | each object | not needed |
| `filesystem` | A read-only directory tree | `filesystem` | each tree | not needed |
| `duckdb` | Parquet, CSV, and DuckDB files | `dataset` | the whole adapter | interrupts the query |
| `git` | A git repository, read-only | `repository` | each repository | kills the git process |
| `browser` | A headless Chromium session (Playwright) | `browser_session` | each session | cancels the page task |
| `room` | A simulated room: a temperature sensor and a heater (`continuous`); recorded windows | `room`, `window` | each room | ends a stream |
| `sim` | A simulation of the room; each state never changes, so a state forks | `sim_state` | each state | checks between steps |
| `graph` | A computation graph (inside the runtime, which does its capabilities) | `graph` | none | reaches the node that runs |
| `api` | A generic HTTP JSON API at a fixed base URL; stored responses | `api`, `response` | each API, each response | closes the socket |

**Streams.** A capability with `execution.mode` `stream` has a `stream(ref, capability, input,
call)` method in its adapter. It yields one bounded value for each event and stops soon after the
call is cancelled. The runtime cancels the call when the subscriber stops.

**Versions and reuse.** A graph reuses a result when the version of a resource did not change. So
an adapter MUST change the version when the state changes. An adapter that cannot know (a live
web page) gives a new version on each observe, and a graph never reuses its results.

**The graph adapter and principals.** An adapter never sees a principal. The graph adapter is the
exception, like the job adapter: the runtime does its capabilities, because a graph runs
operations for a principal.

