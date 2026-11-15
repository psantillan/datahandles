"""Prove that the tests can fail.

Each mutant removes one guard from the source. The tests must fail for each mutant.
Run: .venv/bin/python tests/mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests"
RT, FS, DB = "packages/runtime/runtime.py", "packages/adapters/filesystem/__init__.py", "packages/adapters/duckdb/__init__.py"
REG, JOBS, WORKER, HTTP = "packages/runtime/registry.py", "packages/runtime/jobs.py", "packages/workers/remote.py", "packages/protocols/http.py"
ADP, PLUG, GIT, WEB = "packages/runtime/adapter.py", "packages/runtime/plugins.py", "packages/adapters/git/__init__.py", "packages/adapters/browser/__init__.py"

# (name, file, text to find, replacement)
MUTANTS = [
    ("no grant check on a capability", RT, "        if not _permits(held, name):\n            raise", "        if False:\n            raise"),
    ("a handle id alone gives access", RT, "if rec is None or not self._held(principal, rec):", "if rec is None:"),
    ("a derived handle gives every capability", RT, "inherited = p if ALL in inherited else inherited if ALL in p else inherited & p", "pass"),
    ("no grant check on a second input", RT, "if not _allows(self._held(principal, other), capability):", "if False:"),
    ("a principal can grant what it does not hold", RT, "        if missing:\n", "        if False:\n"),
    ("revoke does nothing", RT, "        self.registry.revoke(from_, rec.id)", "        pass"),
    ("discovery lists every capability", RT, "self._discover(rec) if _permits(held, c[\"name\"])]", "self._discover(rec)]"),
    ("no output size check in the runtime", RT, "if size > limit:\n                        raise HandleError(\"RESULT_TOO_LARGE\", f\"the output", "if False:\n                        raise HandleError(\"RESULT_TOO_LARGE\", f\"the output"),
    ("no input schema check", RT, "            check_input(cap[\"input_schema\"], operation[\"input\"])\n", ""),
    ("release does not end derived handles", RT, "rec.released_at is not None or any(", "rec.released_at is not None or False and any("),
    ("an adapter fault stops the runtime", RT, "            except Exception as e:  # an adapter fault must not stop the runtime\n                raise HandleError(\"ADAPTER_ERROR\", f\"{type(e).__name__}: {e}\") from e\n", ""),
    ("submit runs nodes in list order", RT, "blocked = {}, {}, {}, set()\n        for nid in topological_order(graph):", "blocked = {}, {}, {}, set()\n        for nid in [n['id'] for n in graph['nodes'] if topological_order(graph)]:"),
    ("ephemeral handles survive a restart", RT, "        self.registry.drop_ephemeral()\n", ""),
    ("no cycle detection", "packages/core/graphs.py", "if len(order) != len(ids):", "if False:"),
    ("no path containment", FS, "if path != root and root not in path.parents:", "if False:"),
    ("search follows a symlink out of the root", FS, "if root not in file.resolve().parents:", "if False:"),
    ("head does not stop at the size limit", DB, "            if budget < 0:\n", "            if False:\n"),
    ("eager does not store a result", DB, "        if not eager:\n", "        if True:\n"),
    ("no column check", DB, "        if missing:\n", "        if False:\n"),
    ("a snapshot is not a copy", "packages/adapters/memory/__init__.py", "copy.deepcopy(self._at(ref))", "self._at(ref)"),
    ("the handle list shows the handles of other principals", RT, "for rec in recs if self._held(principal, rec)]", "for rec in recs]"),
    ("a grant on the target gives the job handle", RT, "if rec.parents and rec.kind != \"job\":", "if rec.parents:"),
    ("another principal gets a job by its operation id", RT, "job[\"principal\"] == principal and job[\"operation\"] == operation", "job[\"operation\"] == operation"),
    ("an operation id is used for two jobs", RT, "            if self.registry.job(operation[\"id\"]):\n", "            if False:\n"),
    ("a second submission does not give the same job", RT, "            job = self._duplicate(principal, operation)\n            if job:\n", "            job = None\n            if job:\n"),
    ("execution.mode does not limit the place", RT, "if execution[\"mode\"] not in (\"auto\", placed):", "if False:"),
    ("two input handles can be in two places", RT, "            if other.adapter != rec.adapter:\n", "            if False:\n"),
    ("the handle of a cancelled job stays", RT, "if state != result[\"status\"] and \"handles\" in result:", "if False:"),
    ("an interrupted job that is not idempotent runs again", RT, "if job[\"capability\"][\"execution\"][\"idempotent\"] and not job[\"cancel\"]:", "if True:"),
    ("an interrupted job does not run again", RT, "                self.registry.requeue_job(job[\"id\"])\n", "                pass\n"),
    ("a late attempt ends the job", REG, " or job[\"attempts\"] != attempt:", ":"),
    ("a cancelled running job keeps its result", REG, "            if job[\"cancel\"]:\n", "            if False:\n"),
    ("cancel does not end a queued job", REG, "SET state = 'cancelled', finished_at = ? WHERE id = ? AND state = 'queued'", "SET finished_at = ? WHERE id = ? AND state = 'queued'"),
    ("a job gives a result before its end", JOBS, "        if state not in TERMINAL:\n", "        if False:\n"),
    ("a worker does a task with a bad signature", WORKER, "if not hmac.compare_digest(sign(self.key, body), signature or \"\"):", "if False:"),
    ("a worker does an expired task", WORKER, "if task[\"expires\"] < now:", "if False:"),
    ("a worker does a task two times", WORKER, "if task[\"id\"] in self._seen:", "if False:"),
    ("a lost worker stops the runtime", WORKER, "    except (OSError, http.client.HTTPException, ValueError) as e:\n        raise HandleError(\"WORKER_UNAVAILABLE\", f\"no answer from the worker at {url}: {type(e).__name__}: {e}\", retryable=True) from e\n", "    finally:\n        pass\n"),
    ("the HTTP server accepts each token", HTTP, "principal = self._principals.get(_digest(token)) if kind == \"Bearer\" else None", "principal = self._principals.get(_digest(token), \"paul\")"),
    ("the HTTP server accepts unknown parameters", HTTP, "if not isinstance(params, dict) or set(params) - set(required) - set(optional):", "if False:"),
    ("each HTTP principal can create a resource handle", HTTP, "if method == \"handle.create\" and principal not in self.creators:", "if False:"),
    # v0.2: locks, job threads, cancel, time limits, installed adapters, git, browser
    ("one lock for all resources of an adapter", ADP, "key = None if ref is None else self._adapter.lock_key(ref)", "key = None"),
    ("calls on one resource do not take turns", ADP, "            with self._lock(ref):\n                return attr(*args, **kwargs)", "            return attr(*args, **kwargs)"),
    ("a hook after the cancel never runs", ADP, "        hook()\n        return lambda: None", "        return lambda: None"),
    ("only one job thread", RT, "while len(self._threads) < self._job_threads:", "while not self._threads:"),
    ("job.cancel does not reach a running call", JOBS, "            self._cancel_running(ref.locator[\"job\"])\n", ""),
    ("no time limit", RT, "timeout_ms = cap.get(\"limits\", {}).get(\"timeout_ms\")", "timeout_ms = None"),
    ("a time limit does not cancel the call", RT, "            call.cancel(\"timeout\")\n", ""),
    ("a worker ignores a cancel task", WORKER, "                running.cancel(task[\"reason\"])", "                pass"),
    ("an entry point replaces a built-in adapter", PLUG, "if importlib.util.find_spec(f\"datahandles.adapters.{kind}\") is not None:", "if False:"),
    ("a git diff does not stop at the size limit", GIT, "            while len(json.dumps(cut).encode()) > budget:", "            while False:"),
    ("a cancel does not kill the git process", GIT, "            call.on_cancel(proc.kill)", "            pass"),
    ("a browser opens any URL scheme", WEB, "if urlsplit(url).scheme not in (\"http\", \"https\"):", "if False:"),
    ("a cancel does not reach the browser task", WEB, "            call.on_cancel(future.cancel)", "            pass"),
    # v0.3: graph reuse, actions and approvals, windows, streams, brief describe, DuckDB cancel
    ("a graph never reuses a node", RT, "                before = self.registry.node_run(principal, key)\n", "                before = None\n"),
    ("a principal reuses the node of another principal", RT, "                before = self.registry.node_run(principal, key)\n", "                before = self.registry.node_run(\"paul\", key)\n"),
    ("a node key has no source version", RT, "\"version\": self.observe(principal, node[\"reference\"])[\"version\"]", "\"version\": \"\""),
    ("an action runs with no approval", RT, "if action and self.registry.take_approval(key, op_id, _now()) is None:", "if False:"),
    ("an approval allows more than one run", REG, "            self._all(\"UPDATE approvals SET used_at = ?, used_by = ? WHERE seq = ?\", (at, operation_id, rows[0][\"seq\"]))\n", ""),
    ("an action key has the version of its target", RT, "key, op_id = (structural[nid] if action else keys[nid]), new_id(\"op\")", "key, op_id = keys[nid], new_id(\"op\")"),
    ("a failed node does not block the nodes after it", RT, "            if any(e[\"from\"] in blocked for e in sources):", "            if False:"),
    ("a graph cancel does not reach its nodes", RT, "\"strategy\": \"eager\" if eager else \"lazy\"}}, queue=False, call=call, within=within)", "\"strategy\": \"eager\" if eager else \"lazy\"}}, queue=False, call=None, within=within)"),
    ("a deterministic node keeps the source version", RT, "                if deterministic:", "                if False:"),
    ("a stream capability runs through invoke", RT, "            if cap[\"execution\"][\"mode\"] == \"stream\":", "            if False:"),
    ("subscribe takes a capability that is not a stream", RT, "        if cap[\"execution\"][\"mode\"] != \"stream\":", "        if False:"),
    ("a memory object shares the value of the caller", "packages/adapters/memory/__init__.py", "[copy.deepcopy(source[\"value\"]), 1]", "[source[\"value\"], 1]"),
    ("a cancel does not interrupt the DuckDB query", DB, "        remove = call.on_cancel(self.db.interrupt)", "        remove = lambda: None"),
    ("a stream event has no size limit", RT, "                if size > limit:\n                    raise HandleError(\"RESULT_TOO_LARGE\", f\"an event", "                if False:\n                    raise HandleError(\"RESULT_TOO_LARGE\", f\"an event"),
    # after v0.3: the generic API adapter
    ("an API path can leave the base URL", "packages/adapters/api/__init__.py", "        if (not path.startswith(\"/\") or path.startswith(\"//\")", "        if (False and path.startswith(\"//\")"),
    ("a percent escape hides a .. segment in an API path", "packages/adapters/api/__init__.py", "segments = unquote(path).replace(\"\\\\\", \"/\").split(\"/\")", "segments = path.split(\"/\")"),
    ("an API token is not sent", "packages/adapters/api/__init__.py", "                headers[\"Authorization\"] = f\"Bearer {token}\"\n", "                pass\n"),
    ("an API key is not sent in its header", "packages/adapters/api/__init__.py", "                headers[header] = token\n", "                pass\n"),
    ("an API key in the query string shows in the URL of a stored response", "packages/adapters/api/__init__.py",
     "\"url\": f\"{base.scheme}://{base.netloc}{target}\"", "\"url\": f\"{base.scheme}://{base.netloc}{wire}\""),
    ("an API key in the query string shows in the location of a redirect", "packages/adapters/api/__init__.py",
     "                if secret and result[\"location\"]:", "                if False:"),
    ("a caller sets the key parameter of an API", "packages/adapters/api/__init__.py", "                if param in (query or {}):", "                if False:"),
    ("a token value with a line end shows in an error", "packages/adapters/api/__init__.py",
     "            if not (token.isascii() and token.isprintable()):", "            if False:"),
    ("an API registration names two places for its key", "packages/adapters/api/__init__.py", "        if header and param:", "        if False:"),
    ("a holder cannot read its own log rows", "packages/runtime/runtime.py", "    return capability == LOG_OWN or _allows(held, capability)", "    return _allows(held, capability)"),
    ("log.own shows the rows of other principals", "packages/runtime/runtime.py", "            filters[\"principal\"] = principal\n", "            pass\n"),
    ("a log cursor from another principal is accepted", "packages/runtime/runtime.py", "(capability == LOG_AUDIT or start[\"principal\"] == principal)", "True"),
    ("a log cursor that is not in the view is accepted", "packages/runtime/runtime.py", "        if after is not None and not (start and visible(start)", "        if False and not (start and visible(start)"),
    ("the audit view does not follow the parents edges", "packages/runtime/runtime.py", "any(self._descends(p, handle_id, memo) for p in rec.parents)", "False"),
    ("a log row made inside a graph run is outside the view of the graph", "packages/runtime/runtime.py", "            row = self.registry.entry(row[\"within\"]) if row[\"within\"] else None", "            row = None"),
    ("a graph node row does not name the run that it was made inside", "packages/runtime/runtime.py", "queue=False, call=call, within=within)", "queue=False, call=call, within=None)"),
    ("a large log input is not cut", "packages/runtime/runtime.py", "    if len(canonical.encode()) <= LOG_INPUT_BYTES:", "    if True:"),
    ("the log input hash is not of the canonical input", "packages/runtime/runtime.py", "hashlib.sha256(canonical.encode())", "hashlib.sha256(row[\"input\"].encode())"),
    ("a log page has no size limit", "packages/runtime/runtime.py", "if len(rows) == limit or size + n > budget:", "if len(rows) == limit:"),
    ("a failed lifecycle method is logged as completed", "packages/runtime/runtime.py", "            status, code = \"failed\", getattr(e, \"code\", \"ADAPTER_ERROR\")\n            raise\n        finally:\n            made", "            raise\n        finally:\n            made"),
    ("a grant writes no log row", "packages/runtime/runtime.py", "return self._lifecycle(principal, \"grant\", handle_id, given, lambda: self._grant(principal, handle_id, to, capabilities))", "return self._grant(principal, handle_id, to, capabilities)"),
    ("a job.result row does not name the handle that the job made", "packages/runtime/runtime.py", "result_handle=(derived or made) and (derived or made).id", "result_handle=derived and derived.id"),
    ("a stream row stays running", "packages/runtime/runtime.py", "            self.registry.finish(entry,", "            dict(entry=entry,"),
    ("a graph reuses a read of the log", "packages/runtime/runtime.py", "                if node[\"operation\"][\"capability\"] in (LOG_OWN, LOG_AUDIT):  # the log grows while the version of the handle stays\n                    before = None\n", "                if node[\"operation\"][\"capability\"] in (LOG_OWN, LOG_AUDIT):  # the log grows while the version of the handle stays\n                    pass\n"),
    ("an action that waits writes no log row", "packages/runtime/runtime.py", "                    if not before:  # the log shows an action that waits", "                    if False:  # the log shows an action that waits"),
    ("a registry from v0.3 gets no new log columns", "packages/runtime/registry.py", "            if name not in columns:", "            if False:"),
    ("an API response has no size limit", "packages/adapters/api/__init__.py", "            if len(raw) > limit:", "            if False:"),
    ("a live API answer has a stable version", "packages/adapters/api/__init__.py", "else new_id(\"v\")}", "else \"\"}"),
    ("a live API answer has a body of any size", "packages/adapters/api/__init__.py", "        elif len(json.dumps(parsed).encode()) <= DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE:", "        elif True:"),
    ("api.fetch stores an error response", "packages/adapters/api/__init__.py", "            if not 200 <= result[\"status\"] < 300:", "            if False:"),
    ("an API connect has no time limit", "packages/adapters/api/__init__.py", "(base.netloc, timeout=timeout_ms / 1000 if timeout_ms else None)", "(base.netloc)"),
    ("a cancelled API call still sends its request", "packages/adapters/api/__init__.py", "                call.check()  # a cancel came during the connect: send nothing\n", "                pass\n"),
    ("the brief describe gives every schema", RT, "        elif detail == \"brief\":", "        elif False:"),
]


def main() -> int:
    survivors = 0
    for name, file, old, new in MUTANTS:
        path = ROOT / file
        source = path.read_text()
        assert source.count(old) == 1, f"{name}: the text must be in {file} exactly one time"
        try:
            path.write_text(source.replace(old, new))
            run = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "-x", "-p", "no:cacheprovider"],
                                 cwd=ROOT, capture_output=True, text=True)
        finally:
            path.write_text(source)
        killed = run.returncode == 1  # 1 = a test failed. Each other code is a survivor or a broken run.
        survivors += not killed
        failed = next((line.split("::")[1].split(" ")[0] for line in run.stdout.splitlines() if line.startswith("FAILED")), "-")
        print(f"{'killed  ' if killed else 'SURVIVED'}  {name}  [{failed}]")
    print(f"{len(MUTANTS) - survivors} of {len(MUTANTS)} mutants killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
