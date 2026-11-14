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

# (name, file, text to find, replacement)
MUTANTS = [
    ("no grant check on a capability", RT, "        if not _allows(held, name):\n            raise", "        if False:\n            raise"),
    ("a handle id alone gives access", RT, "if rec is None or not self._held(principal, rec):", "if rec is None:"),
    ("a derived handle gives every capability", RT, "inherited = p if ALL in inherited else inherited if ALL in p else inherited & p", "pass"),
    ("no grant check on a second input", RT, "if not _allows(self._held(principal, other), capability):", "if False:"),
    ("a principal can grant what it does not hold", RT, "        if missing:\n", "        if False:\n"),
    ("revoke does nothing", RT, "        self.registry.revoke(from_, rec.id)", "        pass"),
    ("discovery lists every capability", RT, "discover(rec.ref) if _allows(held, c[\"name\"])]", "discover(rec.ref)]"),
    ("no output size check in the runtime", RT, "if size > limit:", "if False:"),
    ("no input schema check", RT, "            check_input(cap[\"input_schema\"], operation[\"input\"])\n", ""),
    ("release does not end derived handles", RT, "rec.released_at is not None or any(", "rec.released_at is not None or False and any("),
    ("an adapter fault stops the runtime", RT, "                except Exception as e:  # an adapter fault must not stop the runtime\n                    raise HandleError(\"ADAPTER_ERROR\", f\"{type(e).__name__}: {e}\") from e\n", ""),
    ("submit runs nodes in list order", RT, "for nid in topological_order(graph):", "for nid in [n['id'] for n in graph['nodes'] if topological_order(graph)]:"),
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
