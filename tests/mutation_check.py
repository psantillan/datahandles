"""Prove that the acceptance tests can fail.

Each mutant removes one guard from the source. The acceptance tests must fail for each mutant.
Run: .venv/bin/python tests/mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/conformance/test_acceptance.py"
RT, FS, DB = "packages/runtime/runtime.py", "packages/adapters/filesystem/__init__.py", "packages/adapters/duckdb/__init__.py"

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
    ("an adapter fault stops the runtime", RT, "            except Exception as e:  # an adapter fault must not stop the runtime\n                raise HandleError(\"ADAPTER_ERROR\", f\"{type(e).__name__}: {e}\") from e\n", ""),
    ("submit runs nodes in list order", RT, "for nid in topological_order(graph):", "for nid in [n['id'] for n in graph['nodes'] if topological_order(graph)]:"),
    ("ephemeral handles survive a restart", RT, "        self.registry.drop_ephemeral()\n", ""),
    ("no cycle detection", "packages/core/graphs.py", "if len(order) != len(ids):", "if False:"),
    ("no path containment", FS, "if path != root and root not in path.parents:", "if False:"),
    ("search follows a symlink out of the root", FS, "if root not in file.resolve().parents:", "if False:"),
    ("head does not stop at the size limit", DB, "            if budget < 0:\n", "            if False:\n"),
    ("eager does not store a result", DB, "        if not eager:\n", "        if True:\n"),
    ("no column check", DB, "        if missing:\n", "        if False:\n"),
    ("a snapshot is not a copy", "packages/adapters/memory/__init__.py", "copy.deepcopy(self._at(ref))", "self._at(ref)"),
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
