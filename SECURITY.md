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
- The git adapter is read-only. It reads commits and trees, never the work tree. Git resolves each
  path inside a commit (`commit:path`). A rev goes to git after `--end-of-options`, so it cannot
  pass as an option. External diff programs and text conversion are off.
- The browser adapter opens only http and https URLs.
- The api adapter sends each request to the host and the path of the base URL that the
  registering principal named. A caller names only a path: a path with a host, a scheme, a query
  string, or a `..` segment is refused, also when the segment is written with percent escapes
  (`%2e%2e`) or backslashes. A redirect is not followed. A token comes from an environment
  variable of the process that runs the adapter (`token_env`) and never goes into the registry.
  A call that is cancelled before the connection is ready sends no request.
- A key that the api adapter sends in the query string (`token_query`) stays out of the URL of a
  stored response, out of each error message, and out of the `location` of a redirect. A caller
  cannot set that query parameter. A token value with a control character gives an error that
  does not show the value.
- Each value output has a size limit that the runtime enforces.
- The HTTP transport takes the principal from a bearer token, never from the request body. Only
  principals that the operator lists can create a resource handle.
- A task to a worker carries an HMAC-SHA256 signature, an expiry time, and an id that the worker
  accepts one time. A worker hosts only the adapters that its operator lists.
- All checks run again when a job starts, so a revoked grant stops a queued job.
- In a graph, an action (a capability that writes, reaches outside, and is not idempotent) runs
  only with an approval, and one approval allows one run. Approving is a separate grant
  (`graph.approve`) from running (`graph.run`).
- An input error gives the input schema of the capability, which the caller can already see
  with `handle.describe`. It never gives data.

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
- The base URL of an api handle can be any address that the runtime process can reach, local
  network addresses included. Give the right to register only to trusted principals.
- The api adapter sends its token to a plain `http` base URL too. Use an `https` base URL with a token.
- The api adapter does not search a response body for the key. A service that repeats the request
  URL or its headers in a body shows the key to the caller.
- A key in the query string can go into the access log of the service or of a proxy. Use
  `token_header` when the service accepts a header.
- A browser session can reach every http and https address that the runtime or worker process can
  reach, local network addresses included. Give the browser adapter only to trusted principals, or
  run it on a worker in a network that holds nothing private.
- A time limit (`limits.timeout_ms`) and a cancel are cooperative. An adapter that does not honour
  them keeps running and keeps its lock. There are no resource quotas for one operation.
