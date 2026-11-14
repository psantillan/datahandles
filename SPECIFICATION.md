# DataHandles Protocol Specification

Version 0.1 (draft). The key words MUST, MUST NOT, and MAY have the meanings of RFC 2119.

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
- `type` names the resource type (`dataset`, `filesystem`, `object`). It is the prefix of each
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

## 6. Result

Schema: `spec/protocol/results.schema.json`.

- `status` `completed`: the result has `output` (capability returns a value) or `handles`
  (capability returns a handle), not both.
- `status` `failed`: the result has `error` (`errors.schema.json`) and has no `output`.
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

## 8. Authorization

- A grant names a principal, a handle, and capability names (or `*` for all).
- The principal that registers a resource is its owner and holds `*` on it.
- A principal MAY grant to another principal only capabilities that it holds itself.
- On a derived handle, a principal holds: the grants made on that handle, plus the capabilities
  that it holds on **every** parent. A derivation therefore gives no more authority than its
  inputs.
- An operation with a `$handle` input needs the same capability on that input handle.
- For each operation the runtime MUST do these checks in this order:
  1. Take the authenticated principal from the transport.
  2. Resolve the handle. If the principal holds no capability on it, the answer MUST be
     `HANDLE_NOT_FOUND`, the same as for an id that does not exist.
  3. Check that the handle and its parents are not released (`HANDLE_RELEASED`).
  4. Check that the capability exists (`CAPABILITY_NOT_FOUND`) and that the principal holds it
     (`CAPABILITY_DENIED`).
  5. Validate the input (`INVALID_INPUT`).
  6. Resolve and check each `$handle` input.
  7. Call the adapter.
  8. Record the operation: principal, target, capability, input, status, duration, bytes, version.
- An adapter never sees a principal or a grant. An adapter MUST keep each operation inside the
  resource of the handle (for example, a filesystem adapter MUST NOT follow a path or a symlink
  out of the root).
- A caller supplies names and values. An adapter MUST NOT run query text from a caller.

## 9. Consistency

| Value | Meaning |
|---|---|
| `latest` | Each operation reads the current state of the resource. |
| `snapshot` | The handle is pinned to one version. Later changes to the resource do not change its answers. |
| `continuous` | A stream of changes. Reserved. Not in a v0.1 runtime. |

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

## 13. Not in the v0.1 runtime yet

The schemas reserve these. The reference runtime does not implement them. See `ROADMAP.md`.

- Result statuses `running`, `waiting`, `cancelled`, and the job state machine (all operations are synchronous).
- `execution.mode` `remote` and worker dispatch (all operations run in the runtime process).
- `execution.priority`, `execution.deadline`, `limits.timeout_ms`, `lifecycle.expires_at`.
- `continuous` consistency and subscriptions.
- Transports (HTTP, SSE, MCP). The only transport is the in-process Python API, and the caller
  vouches for the principal name.
