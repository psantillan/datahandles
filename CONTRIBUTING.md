# Contributing

## Set up

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Before you send a change

1. Run the acceptance tests: `.venv/bin/python -m pytest tests/conformance/test_acceptance.py -q`
2. Run the mutation check: `.venv/bin/python tests/mutation_check.py`

The mutation check removes one guard at a time and confirms that a test fails. If you add a
guard (a grant check, a size limit, a containment check), add a mutant for it. A guard with no
test that can fail is not done.

## Rules for a change

- The protocol comes first. A change to behavior starts in `SPECIFICATION.md` and `spec/protocol/`.
- Keep the core small. A new resource type is a new adapter in `packages/adapters/`. It must not
  need a change to `packages/core/` or `packages/runtime/`.
- A capability that returns a value must have an output size limit.
- An adapter must not run query text from a caller.
- Each numeric limit needs a stated origin: a specification, a measurement, or the word ASSUMED.
- Report measurements, with the command that made them.
