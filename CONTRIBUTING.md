# Contributing

## Set up

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m playwright install --only-shell chromium
```

The browser tests need the headless browser. The git tests need the `git` program.

## Before you send a change

1. Run the tests: `.venv/bin/python -m pytest tests -q`
2. Run the mutation check: `.venv/bin/python tests/mutation_check.py`

The mutation check removes one guard at a time and confirms that a test fails. If you add a
guard (a grant check, a size limit, a containment check), add a mutant for it. A guard with no
test that can fail is not done.

## Rules for a change

- The protocol comes first. A change to behavior starts in `SPECIFICATION.md` and `spec/protocol/`.
- A test in `tests/conformance/` uses protocol methods only (the `Client` and the HTTP binding).
  It must not read the registry or call an adapter, because it must run against any implementation.
- Keep the core small. A new resource type is a new adapter: in `packages/adapters/`, or in a
  separate package with an entry point in the group `datahandles.adapters`. It must not need a
  change to `packages/core/` or `packages/runtime/`.
- An adapter whose calls can take long must honour a cancel (`SPECIFICATION.md` section 18), and
  must say in `lock_key` which calls can run at the same time.
- A capability that returns a value must have an output size limit.
- An adapter must not run query text from a caller.
- Each numeric limit needs a stated origin: a specification, a measurement, or the word ASSUMED.
- Report measurements, with the command that made them.
