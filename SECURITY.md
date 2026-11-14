# Security

## Report a vulnerability

Open a private security advisory on the GitHub repository. Do not open a public issue for a
vulnerability.

## What the runtime protects

- A handle id gives no authority. Each operation is checked against the grants of the principal.
- A derived handle gives no more authority than the handles it derives from.
- An operation input is validated against the schema of its capability before an adapter runs.
- An adapter receives names and values, never query text. The DuckDB adapter quotes each
  identifier, checks each column against the schema, and binds each value as a parameter.
- The filesystem adapter is read-only and keeps each path, symlinks included, inside its root.
- Each value output has a size limit that the runtime enforces.
- The HTTP transport takes the principal from a bearer token, never from the request body. Only
  principals that the operator lists can create a resource handle.
- A task to a worker carries an HMAC-SHA256 signature, an expiry time, and an id that the worker
  accepts one time. A worker hosts only the adapters that its operator lists.
- All checks run again when a job starts, so a revoked grant stops a queued job.

`tests/mutation_check.py` removes each of these guards in turn and confirms that a test fails.

## What it does not protect yet

- The in-process Python API trusts the principal name that the caller gives.
- The HTTP transport and the worker use plain HTTP. A token or a task body is readable on the
  network. Put both behind TLS or on a private network.
- The registry file holds the key of each worker. The tokens file holds each token. Protect both
  files.
- There is no sandbox. An adapter runs in the runtime process, or in the worker process, with its full rights.
- A principal that can register a resource can point an adapter at any path that the runtime
  process can read. Give the right to register only to trusted principals.
- There are no timeouts and no resource quotas for one operation.
