# DataHandles: Architecture and Technical Specification

> This file is the design document that started the project, as written.
> `SPECIFICATION.md` is the normative protocol text. Where the two differ, `SPECIFICATION.md` governs.
> The release plan in section 14 changed after v0.1: v0.2 is now "Any resource" (stateful,
> non-dataset adapters), and the distributed and temporal items moved later. `ROADMAP.md` has the current plan.

Architecture direction established

The three decisions change the nature of this project considerably.

We are no longer designing a data-access library. We are designing a universal, composable resource abstraction with a distributed execution runtime.

I would make one important architectural distinction from the beginning:

A handle is a reference to an addressable resource, not the resource itself and not necessarily an executable operation.

The system surrounding it provides identity, capabilities, execution, composition, state management and lifecycle.

This distinction gives us a foundation that can represent a database table, a running simulation, a filesystem, a browser session, a software repository or a remote agent without requiring every resource to behave like a database.

The project should have three independently useful deliverables:

* A protocol: How agents describe, reference and operate on resources.
* A runtime: How references are resolved and operations executed.
* An SDK: How developers expose resources and compose operations without implementing the protocol themselves.

The protocol should be the durable intellectual property of the project. The runtime and SDK are reference implementations.

## 1. Architectural thesis

The resource is external; the computation is addressable

Traditional programming generally assumes that a program can access the objects and memory it needs, subject to its execution environment.

AI agents operate under a different constraint: their working context is limited, expensive and transient.

We should not attempt to make the context window bigger by transferring more information into it.

Instead, we should give the agent a persistent, structured way to refer to external state and request computation against that state.

The proposed interaction model:

Agent

Plans and reasons over observations

Handle Protocol

Resolve

Resource identity

Discover

Available capabilities

Invoke

Typed operations

Compose

Derived references

Observe

Results and changes

Control

Lifecycle and access

Local Runtime

Same-machine execution

Remote Runtime

Distributed execution

Universal Resource Adapters

Databases · Filesystems · APIs · Simulations · Streams · Applications · Agents

The agent does not need to know where a resource lives, how it is stored, or which execution engine operates on it.

It needs to know:

* What the resource represents.
* What it can do.
* What operations are permitted.
* What guarantees those operations provide.
* What reference is returned after execution.

That is the contract.

## 2. Core architecture: five primitives

I would build the entire system around five fundamental primitives.

| Primitive | Responsibility |
|---|---|
| Resource | The external thing being represented |
| Handle | The logical reference to a resource |
| Capability | The authority to perform specific operations |
| Operation | A typed request to perform computation or an action |
| Result | The observation or new reference produced by execution |

These should be independent concepts in the protocol.

### 2.1 Resource

A resource is anything that can be addressed and interacted with.

It does not have to be data.

Examples:

| Resource type | Possible operations |
|---|---|
| Dataset | Filter, aggregate, join, sample |
| Filesystem | List, read, search, write |
| Repository | Inspect, diff, search, test |
| Browser session | Navigate, inspect, extract |
| Simulation | Step, inspect state, evaluate |
| Sensor stream | Subscribe, aggregate, await |
| Agent | Inspect status, submit task, retrieve result |
| Financial portfolio | Inspect, calculate exposure, compare |

A resource adapter implements the operations appropriate to its resource.

The core runtime does not need to understand the internal structure of every resource type.

### 2.2 Handle

A handle identifies a resource or computational object.

I would define the canonical handle as:

```ts
interface Handle {
  id: string;
  type: string;
  version: string;
  descriptor: {
    name?: string;
    description?: string;
    schema?: unknown;
  };
  capabilities: Capability[];
  consistency: ConsistencyModel;
  lifecycle: {
    created_at: string;
    expires_at?: string;
    persistence: "ephemeral" | "persistent";
  };
}
```

One correction to the initial concept: the handle should not carry the actual authorization secret.

It identifies the resource and describes its permitted interface. Authorization must be checked independently by the runtime.

This prevents a copied handle from automatically becoming a transferable security credential.

Three handle categories

#### 1. Resource Handle

References an external resource.

Example: a Postgres table or running browser session.

#### 2. Computation Handle

References a planned or executing computation.

Example: a filter-and-aggregate graph waiting for execution.

#### 3. Result Handle

References an output produced by a computation.

Example: a filtered dataset, simulation checkpoint or generated report.

These three categories should share a common reference interface, but retain distinct semantics.

### 2.3 Capability

A capability defines the operations available against a resource.

I would use operation-level capability descriptors.

```ts
interface Capability {
  name: string;
  description: string;
  input_schema: JSONSchema;
  output_schema: JSONSchema;
  effects: {
    read: boolean;
    write: boolean;
    external: boolean;
  };
  execution: {
    mode: "sync" | "async" | "stream";
    idempotent: boolean;
  };
  limits?: {
    timeout_ms?: number;
    max_output_bytes?: number;
    max_items?: number;
  };
}
```

The effects declaration is important.

An agent should be able to distinguish:

* Reading a resource.
* Mutating a resource.
* Triggering an external side effect.
* Starting a long-running process.

We should eventually support capability composition and delegation, but not make that a requirement for the first release.

### 2.4 Operation

Operations are the execution requests.

The operation interface should be declarative, typed and serializable.

```ts
interface Operation {
  id: string;
  target: string;
  capability: string;
  input: Record<string, unknown>;
  execution: {
    mode: "auto" | "local" | "remote";
    priority?: number;
    deadline?: string;
  };
}
```

The runtime validates the request against the target handle’s capability descriptor before dispatch.

An operation should never implicitly grant more authority than the caller already possesses.

### 2.5 Result

A result is not necessarily a data payload.

It can be:

* A scalar.
* A structured observation.
* A new handle.
* A stream cursor.
* A job reference.
* A checkpoint.
* An error with machine-readable recovery information.

```ts
interface OperationResult {
  operation_id: string;
  status:
    | "completed"
    | "running"
    | "waiting"
    | "failed"
    | "cancelled";
  output?: unknown;
  handles?: Handle[];
  metadata: {
    execution_time_ms?: number;
    bytes_transferred?: number;
    source_version?: string;
  };
}
```

This is the critical mechanism that prevents intermediate data from having to pass through the agent.

## 3. Composable computation model

This is where I would invest the most design effort.

The system should support a handle producing another handle, which can then be used as the input to another operation.

This creates a composable graph of resource references and computations.

### 3.1 The computation graph

Represent the graph as a directed acyclic graph (DAG).

Each node is one of:

* External resource.
* Operation.
* Derived resource.
* Materialized result.

Each edge represents a dependency or data relationship.

For example:

Resource A

Customer event collection

Operation A

Filter: event_type = purchase

Derived Handle B

Logical reference to filtered data

Operation B

Group by customer, aggregate revenue

Result Handle C

Customer revenue distribution

The agent can continue operating on C without knowing how A was stored or how B was executed.

Graph representation

I would define a portable graph format:

```ts
interface ComputationGraph {
  id: string;
  nodes: ComputationNode[];
  edges: ComputationEdge[];
  execution: {
    strategy: "lazy" | "eager" | "auto";
    consistency: "snapshot" | "latest";
  };
}
interface ComputationNode {
  id: string;
  kind:
    | "resource"
    | "operation"
    | "derived"
    | "materialized";
  reference?: string;
  operation?: Operation;
}
interface ComputationEdge {
  from: string;
  to: string;
  input_name?: string;
}
```

The graph is a declarative description, not executable code.

The runtime decides how to execute it.

Why this matters

It gives us the possibility of:

* Parallel execution of independent branches.
* Query-plan optimization.
* Deduplicating identical operations.
* Caching intermediate results.
* Remote execution of individual subgraphs.
* Resuming incomplete computations.
* Tracking data provenance.

I would require cycle detection for ordinary computation graphs. Stateful feedback loops should be represented as separate long-running processes with explicit state transitions, not silently permitted inside the DAG.

### 3.2 Lazy versus eager execution

These should be explicit behaviors.

Lazy execution

An operation creates a derived handle without immediately executing the entire computation.

Eager execution

An operation executes immediately and returns a result or materialized handle.

Automatic execution

The runtime decides based on operation type, estimated cost, dependencies and resource constraints.

The agent should be able to request a particular mode, but the runtime should retain authority over execution safety and resource limits.

## 4. Local and distributed execution

I would use a common execution interface with multiple execution backends.

The important design principle is:

Execution location must be an implementation detail, not a different programming model.

An operation submitted to a local runtime and the same operation submitted to a remote runtime should have identical semantics, subject to explicitly declared consistency and capability differences.

### 4.1 Runtime architecture

Agent / SDK

Handle requests and computation graphs

Runtime Gateway

Authentication · Authorization · Validation · Routing

Execution Coordinator

Planner

Scheduler

Registry

Policy engine

Local workers

In-process, subprocess, local containers

Remote workers

Networked execution nodes and cloud workers

Resource adapters

Resolve and operate on underlying resources

### 4.2 Execution placement

Every resource should advertise its execution characteristics.

For example:

```json
{
  "resource": "r_customer_events",
  "placement": {
    "local_available": true,
    "remote_available": true,
    "preferred": "local",
    "data_locality": "worker_01"
  }
}
```

The scheduler should consider:

* Where the data resides.
* Whether the worker supports the operation.
* Whether the worker has the required capabilities.
* Expected execution cost.
* Network transfer cost.
* Security and isolation requirements.
* Current worker availability.

A 10 GB dataset should not be transferred to a remote worker simply because that worker has spare CPU capacity.

Ideally, the computation moves to the data.

### 4.3 Worker contract

A worker is an execution environment that advertises supported operation types and resource constraints.

```ts
interface Worker {
  id: string;
  capabilities: string[];
  resources: {
    cpu?: number;
    memory_bytes?: number;
    disk_bytes?: number;
  };
  execute(
    operation: Operation
  ): Promise<OperationResult>;
}
```

The actual worker protocol should support:

* Operation submission.
* Execution status.
* Result retrieval.
* Cancellation.
* Heartbeats.
* Lease renewal.
* Worker registration.
* Worker failure detection.

For the first version, I would implement a local worker and a remote HTTP worker. Do not introduce Kubernetes or a complex distributed scheduler prematurely.

### 4.4 Remote execution security

Remote workers must not automatically receive unrestricted access to the originating runtime.

They should receive scoped, short-lived authorization to execute a particular operation against a particular resource.

This is especially important when the runtime is handling:

* Customer data.
* Private repositories.
* Financial records.
* Browser sessions.
* Credentials.
* External write operations.

For arbitrary third-party code, use isolated execution environments. Do not treat a worker process as a security boundary merely because it is remote.

## 5. Universal resource abstraction

The hardest problem is supporting universal resources without creating an overly generic, meaningless API.

My recommendation is to define a small common interface and allow resource-specific capability extensions.

### 5.1 Common resource interface

Every resource adapter should implement:

```ts
interface ResourceAdapter {
  describe(
    handle: Handle
  ): Promise<ResourceDescription>;
  discover(
    handle: Handle
  ): Promise<Capability[]>;
  invoke(
    handle: Handle,
    operation: Operation
  ): Promise<OperationResult>;
  observe(
    handle: Handle,
    options?: ObserveOptions
  ): Promise<Observation>;
  release(
    handle: Handle
  ): Promise<void>;
}
```

These methods have distinct responsibilities:

* describe: What is this thing?
* discover: What can I do with it?
* invoke: Perform an operation.
* observe: Inspect current state or changes.
* release: End the handle’s associated lifecycle or lease.

Importantly, release must not imply deletion of the underlying resource.

### 5.2 Resource-specific operations

The runtime should not dictate that every resource supports filtering, aggregation or reading.

A simulation might expose:

```json
{
  "capabilities": [
    "simulation.inspect",
    "simulation.step",
    "simulation.set_parameters",
    "simulation.checkpoint",
    "simulation.restore"
  ]
}
```

A filesystem might expose:

```json
{
  "capabilities": [
    "filesystem.list",
    "filesystem.read",
    "filesystem.search",
    "filesystem.write"
  ]
}
```

The protocol is universal because it standardizes how operations are described, authorized, executed and observed—not because every resource implements identical functionality.

This is a critical distinction.

### 5.3 Resource adapters

I would begin with these adapters:

| Adapter | Purpose | Priority |
|---|---|---|
| In-memory object | Validate fundamental abstractions | P0 |
| Filesystem | General-purpose resource interaction | P0 |
| DuckDB | Large-scale analytical computation | P0 |
| HTTP/API | Remote external resources | P1 |
| PostgreSQL | Transactional data | P1 |
| Event stream | Live asynchronous resources | P1 |
| Simulation | Stateful computational resources | P2 |
| Agent | Delegated computation and task references | P2 |

DuckDB and filesystem support give us a strong demonstration. Simulation and agent adapters demonstrate that this is not just a database abstraction.

## 6. Temporal semantics and asynchronous state

This needs to be part of the original specification, not an extension added later.

A handle may represent a resource that changes continuously.

We need to define what it means to inspect, compute against and subscribe to that resource.

### 6.1 Consistency models

I would support three initial models:

| Model | Meaning |
|---|---|
| Snapshot | Operate against a fixed version of the resource |
| Latest | Resolve against the most recent available state |
| Continuous | Maintain an ongoing computation as the resource changes |

These must be explicit in operations.

For example, a request to aggregate revenue should not silently change meaning because new transactions arrived halfway through execution.

For snapshot queries, the runtime should pin the source version or snapshot identifier.

For latest queries, the result should record which version it actually observed.

For continuous queries, the runtime should return a subscription or process handle.

### 6.2 Asynchronous execution

A long-running operation should not hold open an HTTP request indefinitely.

Instead:

* Agent submits operation.
* Runtime validates and schedules it.
* Runtime returns a job handle.
* Agent checks status, waits or subscribes.
* Runtime returns completion metadata and result handle.

The job itself is a resource.

That means the same handle interface can inspect the job, cancel it, await completion or retrieve its result.

This is a powerful unification: jobs, resources, computations and results can all be addressed using the same fundamental mechanism.

### 6.3 State machine

CREATED

Accepted

VALIDATING

Checking

QUEUED

Waiting

RUNNING

Executing

WAITING

External dependency

COMPLETED

Result available

FAILED

Error recorded

CANCELLED

Execution stopped

Terminal states are immutable. Retry creates a new attempt or execution identity while preserving the original history.

I would also distinguish between execution status and result availability. A computation can finish successfully while its output is still being materialized or published.

## 7. Protocol design

I would make the core protocol independent of MCP.

MCP should be a first-class integration, but not the underlying architecture.

### 7.1 Protocol operations

The initial protocol should define these operations:

| Method | Purpose |
|---|---|
| handle.create | Register or create a resource handle |
| handle.resolve | Resolve an existing handle |
| handle.describe | Inspect resource metadata |
| handle.discover | Discover capabilities |
| operation.invoke | Execute an operation |
| graph.submit | Submit a computation graph |
| job.status | Inspect execution state |
| job.cancel | Cancel an execution |
| result.resolve | Retrieve result metadata or bounded output |
| resource.observe | Inspect changes |
| subscription.create | Subscribe to updates |
| handle.release | Release a reference or lease |

These should have stable schemas and explicit versioning.

### 7.2 Transport

For v1:

* JSON over HTTP for remote execution.
* In-process API for local execution.
* Server-Sent Events for status and observation streams.
* Optional WebSocket transport later.
* MCP adapter for agent integration.

I would use JSON Schema for operation and capability definitions.

For internal data transfer, support Arrow or another efficient binary representation when appropriate. Do not force large data payloads through JSON.

### 7.3 Handle identifiers

Use opaque identifiers rather than embedding storage locations in handles.

For example:

```text
hdl_01K9X8M7...
```

The runtime maintains the mapping:

```text
Handle ID
    |
    +-- Resource identity
    +-- Resource adapter
    +-- Version
    +-- Capabilities
    +-- Ownership and policy
    +-- Placement information
    +-- Lifecycle metadata
```

A handle should remain independent of the underlying storage implementation.

For cross-runtime references, define a separate portable reference envelope containing an issuer, audience, protocol version and resource identity. Do not turn the handle ID into a bearer credential.

## 8. Reference implementation technology

I would intentionally avoid building every component from scratch.

My proposed initial stack:

| Component | Technology | Reason |
|---|---|---|
| Core runtime | Python | Rapid adapter and execution development |
| SDK | Python + TypeScript | Broad developer access |
| API | FastAPI | Typed HTTP interface |
| Schemas | JSON Schema | Language-independent contracts |
| Analytical backend | DuckDB | Local analytical execution |
| Data interchange | Apache Arrow | Efficient columnar transfer |
| Metadata registry | SQLite initially | Simple durable registry |
| Remote execution | HTTP workers | Minimal distributed infrastructure |
| Agent integration | MCP | Existing agent ecosystem |
| Testing | pytest + conformance suite | Behavioral guarantees |

I would also evaluate Substrait for portable query-plan representation rather than inventing a proprietary relational algebra.

However, the universal operation graph must support non-relational operations that Substrait is not designed to represent.

The internal computation model should therefore remain broader than the SQL/query-plan model.

## 9. Security and capability delegation

The project should be capability-oriented from day one.

This is particularly important because handles can refer to resources that have real-world side effects.

A handle to a database is different from a handle to a payment system. A handle to a repository is different from a handle to a deployment environment.

The runtime needs to understand those differences.

### 9.1 Authorization model

I recommend four separate security concepts:

| Concept | Question answered |
|---|---|
| Principal | Who is requesting the operation? |
| Resource policy | Who can access this resource? |
| Capability | What operation is permitted? |
| Execution policy | Under what constraints may it execute? |

An operation should pass through these checks:

```text
Request
  |
  v
Authenticate principal
  |
  v
Resolve handle
  |
  v
Check resource authorization
  |
  v
Check capability
  |
  v
Validate input schema
  |
  v
Check execution policy
  |
  v
Dispatch to worker
  |
  v
Record execution and lineage
```

### 9.2 Delegation

An agent should be able to delegate a restricted handle to another agent or worker.

For example:

Agent A possesses a customer database handle with read and aggregate permissions.

Agent A delegates an aggregate-only capability to Agent B.

Agent B can calculate totals but cannot retrieve customer records or modify the database.

Delegation must be:

* Explicit.
* Scoped to particular resources and operations.
* Time-bounded.
* Revocable.
* Non-escalating.

The delegated authority cannot exceed the authority of the delegating principal.

### 9.3 Side-effect classification

I would classify operations into three categories:

* Read-only: No resource mutation.
* Mutating: Changes internal resource state.
* External side effect: Causes an effect outside the runtime’s controlled resource boundary.

External side effects should require stronger authorization and explicit execution policies.

The runtime should also support idempotency keys, execution audit records and compensation hooks where appropriate.

A retry of a payment operation, for example, must not accidentally charge someone twice.

## 10. Registry, storage and lifecycle

The registry is the system’s directory of addressable resources.

It should not become the storage engine.

This separation matters because the runtime should be able to restart, workers should be replaceable, and resources should exist independently of any particular execution process.

### 10.1 Registry responsibilities

The registry stores:

* Handle identity.
* Resource type.
* Adapter identifier.
* Resource locator.
* Version information.
* Capability descriptors.
* Ownership and access policies.
* Lifecycle metadata.
* Execution placement hints.

It should not store the actual resource data unless the resource itself is metadata.

### 10.2 Handle lifecycle

A handle should have a lifecycle independent of the underlying resource.

For example:

A temporary handle to a persistent database table may expire after an hour. Expiring that handle must not delete the database table.

Likewise, a job handle may remain available after the worker that executed the job has disappeared.

I would define:

| Lifecycle event | Meaning |
|---|---|
| Create | Establish a new reference |
| Resolve | Locate an existing reference |
| Renew | Extend an expiring lease |
| Snapshot | Pin a particular resource version |
| Fork | Create a related independent reference |
| Release | End the reference’s lifecycle |
| Revoke | Remove authorization |
| Expire | Automatically end a temporary reference |

### 10.3 Persistence tiers

Start with two tiers:

Ephemeral

* Temporary computation handles.
* Short-lived previews.
* Session-bound references.
* Temporary worker state.

Persistent

* Registered external resources.
* Materialized results.
* Saved computation graphs.
* Long-running subscriptions.
* Durable checkpoints.

SQLite can handle the initial registry, but the registry interface should allow PostgreSQL or another transactional backend later.

## 11. Repository architecture

I would make this a monorepo with a clearly separated specification and implementation.

The important thing is to avoid letting the Python implementation become the de facto protocol definition.

Here is the structure I would establish:

```text
datahandles/
│
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
│
├── SPECIFICATION.md
├── ROADMAP.md
├── ARCHITECTURE.md
│
├── spec/
│   ├── protocol/
│   │   ├── handles.schema.json
│   │   ├── capabilities.schema.json
│   │   ├── operations.schema.json
│   │   ├── results.schema.json
│   │   ├── graphs.schema.json
│   │   └── errors.schema.json
│   │
│   ├── semantics/
│   │   ├── identity.md
│   │   ├── consistency.md
│   │   ├── execution.md
│   │   ├── lifecycle.md
│   │   └── security.md
│   │
│   └── examples/
│
├── packages/
│   ├── core/
│   │   ├── handles/
│   │   ├── capabilities/
│   │   ├── operations/
│   │   ├── results/
│   │   ├── graphs/
│   │   └── errors/
│   │
│   ├── runtime/
│   │   ├── registry/
│   │   ├── resolver/
│   │   ├── planner/
│   │   ├── scheduler/
│   │   ├── executor/
│   │   ├── policy/
│   │   ├── lineage/
│   │   └── lifecycle/
│   │
│   ├── workers/
│   │   ├── local/
│   │   └── remote/
│   │
│   ├── adapters/
│   │   ├── memory/
│   │   ├── filesystem/
│   │   ├── duckdb/
│   │   ├── http/
│   │   ├── postgres/
│   │   ├── streams/
│   │   └── simulation/
│   │
│   ├── protocols/
│   │   ├── http/
│   │   └── mcp/
│   │
│   └── sdk/
│       ├── python/
│       └── typescript/
│
├── examples/
│   ├── basic-resource/
│   ├── large-dataset/
│   ├── computation-graph/
│   ├── remote-worker/
│   ├── live-stream/
│   └── simulation/
│
├── benchmarks/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── conformance/
    ├── distributed/
    └── security/
```

I would use a versioned protocol specification and conformance tests to ensure different implementations behave consistently.

The repository can initially ship with only Python packages. The TypeScript SDK should follow once the protocol stabilizes.

## 12. Proposed API experience

The API should be extremely simple for developers.

The complexity belongs inside the runtime.

For example:

```python
from datahandles import Runtime
runtime = Runtime.local()
events = runtime.register(
    "customer_events",
    adapter="duckdb",
    source="./events.duckdb"
)
purchases = events.filter(
    event_type="purchase"
)
customer_revenue = purchases.group_by(
    "customer_id"
).aggregate(
    revenue="sum"
)
result = customer_revenue.execute()
print(result.inspect())
```

The important part is that purchases and customer_revenue are not necessarily Python dataframes.

They are handle-backed references.

The SDK provides a convenient way to compose operations without forcing materialization.

The agent-facing interface would look something like:

```python
handle = await agent.resolve("customer_events")
description = await handle.describe()
purchases = await handle.invoke(
    "filter",
    {
        "event_type": "purchase"
    }
)
revenue = await purchases.invoke(
    "group_by",
    {
        "column": "customer_id",
        "aggregate": {
            "revenue": "sum"
        }
    }
)
summary = await revenue.invoke(
    "summarize",
    {
        "max_items": 10
    }
)
```

An important SDK design rule: chaining operations should build a computation graph, not eagerly download and manipulate data in the client.

The client should have an explicit materialization method for cases where developers actually need the underlying result.

## 13. Failure handling and reliability

A distributed resource system needs more than successful execution semantics.

We need predictable behavior when things go wrong.

I would establish the following requirements:

| Failure | Required behavior |
|---|---|
| Handle does not exist | Typed HANDLE_NOT_FOUND error |
| Capability denied | CAPABILITY_DENIED |
| Worker unavailable | Retry or report WORKER_UNAVAILABLE |
| Execution timeout | Record timeout and execution state |
| Resource changed | Detect version conflict |
| Result too large | Return bounded output or result handle |
| Network interruption | Allow job recovery and status inspection |
| Duplicate submission | Enforce idempotency where requested |
| Authorization revoked | Stop future unauthorized operations |

We should also distinguish retrying a transport request from retrying an operation.

The former should generally be safe with an idempotency key. The latter may not be safe if the operation has side effects.

For distributed execution, durable job state must live outside individual worker processes.

## 14. What the first release should and should not do

I would be aggressive about limiting the initial scope.

The project is ambitious enough that an overly broad first release would make it difficult to validate the fundamental idea.

v0.1 — Required

Core reference runtime

* Universal resource handle model.
* Capability discovery.
* Typed operation invocation.
* Handle-to-handle composition.
* DAG representation.
* Lazy and eager execution.
* Local execution.
* Remote HTTP worker.
* Durable handle registry.
* Filesystem and DuckDB adapters.
* Bounded result delivery.
* MCP adapter.
* Python SDK.
* Conformance tests.

v0.2 — Expansion

Distributed and temporal execution

* Remote worker discovery.
* Scheduling and placement.
* Persistent asynchronous jobs.
* Snapshot and latest consistency.
* Event stream adapter.
* Subscriptions and observation.
* Checkpoint and resume.
* PostgreSQL adapter.

v0.3 — Universal resources

Generalized computational resources

* Stateful simulation adapters.
* Agent-as-resource support.
* Capability delegation.
* Cross-runtime references.
* Advanced graph optimization.
* Portable execution plans.
* TypeScript SDK.

Explicit non-goals for v0.1

I would not build:

* A new database.
* A new vector database.
* An LLM or agent framework.
* A proprietary query language.
* A mandatory cloud service.
* A Kubernetes scheduler.
* A universal executable sandbox.
* An automatic reasoning engine.

The runtime should be useful without any LLM.

That last point is especially important. We need to prove that the abstraction is sound independently of whichever AI model happens to consume it.

## 15. GitHub positioning and project identity

I would position this as infrastructure for the next generation of AI systems.

Not a tool that helps an LLM search data, but a layer that changes how an LLM interacts with external computational resources.

Proposed identity

DataHandles

Open-source infrastructure

A universal handle runtime for AI agents.

External resources become addressable, composable computational objects.

GitHub description

A universal, composable resource and execution layer for AI agents. Operate on external data, applications, processes and state through lightweight handles and capability-constrained operations.

Suggested repository

Copy name

github.com/organization/datahandles

The conceptual positioning

I would distinguish the project from four neighboring categories:

| Category | What it provides | What DataHandles adds |
|---|---|---|
| RAG | Retrieved information | Persistent computational references |
| MCP | Tool and resource connectivity | Universal handle and composition semantics |
| Databases | Persistent data and queries | A common interface across heterogeneous resources |
| Workflow engines | Execution orchestration | Agent-addressable resources and intermediate computation handles |

These are complementary technologies, not competitors that need to be replaced.

The project should integrate with them.

The central claim

I would use this as the guiding statement throughout the repository:

An AI agent should not need to possess a resource to operate on it. It should need only a reference, the authority to act, and a well-defined interface through which computation can occur.

That is the fundamental proposition we should validate.

## 16. Acceptance criteria: how we know the architecture works

Before calling v0.1 successful, I would require these demonstrations.

| Test | Expected result |
|---|---|
| External resource | Agent receives a handle without receiving resource contents |
| Capability discovery | Agent can discover permitted operations |
| Bounded computation | Large input produces small, controlled output |
| Composition | An operation can consume a previous result handle |
| Graph execution | Multiple dependent operations execute correctly |
| Local/remote parity | Equivalent operations produce equivalent results |
| Resource independence | A new adapter works without modifying core runtime |
| Context efficiency | Agent context usage remains bounded as resource size grows |
| Persistence | Handles and completed jobs survive runtime restarts |
| Security | Unauthorized operations are rejected |
| Temporal correctness | Snapshot and latest queries have distinguishable semantics |
| Failure recovery | Interrupted jobs can be inspected and recovered where supported |

One benchmark deserves particular emphasis.

Context consumption should scale with the complexity of the question, not the size of the underlying dataset.

That is the project’s defining performance property.

It won’t hold for every possible operation: some tasks genuinely require examining or returning many individual records. But the architecture should make bounded computation the default.

## 17. The architectural risks I would watch

There are four ways this project could accidentally become something less interesting.

#### 1. Becoming an MCP wrapper

If all we do is expose database operations through MCP, we’ve built an adapter, not a new infrastructure abstraction.

#### 2. Becoming a distributed workflow engine

If the DAG scheduler becomes the center of the project, we risk duplicating existing orchestration systems. The graph should serve the handle model, not replace it.

#### 3. Over-generalizing resource operations

Trying to make every resource support generic CRUD, filtering and aggregation will produce a lowest-common-denominator interface.

Keep the common protocol small. Let adapters define their own typed capabilities.

#### 4. Confusing handles with permissions

An opaque identifier is not authorization. A handle should identify a resource; a separately enforced policy determines who can do what.

## 18. My recommended implementation order

I would actually implement the project in this order:

* Write the protocol specification first
    Establish resource identity, handle semantics, capabilities, operation requests, results and lifecycle. Define the schemas before building the runtime.
* Build a minimal local runtime
    Register an in-memory object and filesystem resource. Resolve handles and invoke typed operations.
* Implement composability
    Introduce derived handles and a DAG executor. Prove that intermediate data stays external.
* Add DuckDB
    Demonstrate operations against millions of records without transferring the dataset into the client.
* Add remote execution
    Introduce worker registration, dispatch, asynchronous jobs and result resolution.
* Expose MCP and SDK integrations
    Make the architecture accessible to existing agent frameworks.
* Publish conformance tests and benchmarks
    Prove that the abstraction works across different resource types and execution environments.

One final architectural recommendation

I would explicitly separate three things in the specification:

* Handle Protocol: The language-independent contract for addressing resources and invoking operations.
* Handle Runtime: The implementation that resolves references, enforces capabilities and executes computations.
* Handle SDK: The developer-facing abstraction that makes the protocol easy to use.

This separation is what allows the project to grow beyond its first implementation.

A Rust runtime, Python runtime, browser-based runtime or cloud runtime could all implement the same protocol.

And an agent should be able to use them without needing to know which one is underneath.

My strongest recommendation: Freeze the conceptual model before freezing the implementation. The biggest risk isn’t choosing the wrong database or web framework. It’s choosing an incorrect definition of a handle that later makes universal resources, composability or distributed execution awkward.

The handle semantics are the foundation. Everything else should be replaceable.
