"""Tabular data through DuckDB: Parquet files, CSV files, and tables in a DuckDB database file.

A handle holds a relational plan (JSON), not rows. A capability that returns a handle adds one step
to the plan. A capability that returns a value compiles the plan to one SQL statement, and DuckDB
runs it against the source. The caller gives column names and values only, never SQL text.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

import duckdb

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HANDLE_INPUT, HandleError, capability, new_id
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

_OPS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_FNS = {"count": "count({})", "count_distinct": "count(DISTINCT {})", "sum": "sum({})", "avg": "avg({})", "min": "min({})", "max": "max({})"}
_COL = {"type": "string", "minLength": 1}
_SCALAR = {"type": ["string", "number", "boolean"]}
_CAPS = [
    capability("dataset.schema", "Return the column names and types."),
    capability("dataset.count", "Return the number of rows."),
    capability("dataset.head", "Return the first rows. The output stops at `n` rows or at the size limit and says so.",
               {"n": {"type": "integer", "minimum": 1, "default": 10}}),
    capability("dataset.filter", "Derive a handle to the rows that pass every predicate.", {
        "where": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "required": ["column", "op", "value"], "properties": {
            "column": _COL, "op": {"enum": [*_OPS, "in"]}, "value": {"anyOf": [_SCALAR, {"type": "array", "minItems": 1, "items": _SCALAR}]}}}},
    }, required=["where"], returns="handle"),
    capability("dataset.select", "Derive a handle to a subset of the columns.",
               {"columns": {"type": "array", "minItems": 1, "items": _COL}}, required=["columns"], returns="handle"),
    capability("dataset.aggregate", "Derive a handle to one row for each group, with the metrics as columns.", {
        "group_by": {"type": "array", "items": _COL},
        "metrics": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "required": ["fn", "as"], "properties": {
            "fn": {"enum": list(_FNS)}, "column": _COL, "as": _COL}}},
    }, required=["metrics"], returns="handle"),
    capability("dataset.sort", "Derive a handle to the rows in a given order.", {
        "by": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "required": ["column"], "properties": {
            "column": _COL, "descending": {"type": "boolean", "default": False}}}},
    }, required=["by"], returns="handle"),
    capability("dataset.limit", "Derive a handle to the first `n` rows.",
               {"n": {"type": "integer", "minimum": 1}}, required=["n"], returns="handle"),
    capability("dataset.join", "Derive a handle to the join of this dataset (left) with a second dataset handle (right).", {
        "right": HANDLE_INPUT,
        "on": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "required": ["left", "right"], "properties": {"left": _COL, "right": _COL}}},
        "how": {"enum": ["inner", "left"], "default": "inner"},
    }, required=["right", "on"], returns="handle"),
]
_ENVELOPE = 256  # bytes kept for the JSON keys around the rows of dataset.head


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _plain(value):
    """Make a DuckDB value JSON data: a decimal becomes a number, each other type (date, blob, ...) becomes text."""
    return float(value) if isinstance(value, Decimal) else str(value)


class Adapter(ResourceAdapter):
    name = "duckdb"

    def __init__(self, state_dir):
        self._dir = Path(state_dir) / "duckdb"
        self._dir.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self._dir / "results.duckdb"))  # stored results only
        self._attached: dict[str, str] = {}

    # ---- plan -> SQL --------------------------------------------------------------------------

    def _attach(self, path: str) -> str:
        if path not in self._attached:
            alias = f"src{len(self._attached)}"
            self.db.execute(f"ATTACH {_lit(path)} AS {alias} (READ_ONLY)")
            self._attached[path] = alias
        return self._attached[path]

    def _sql(self, plan: dict, params: list) -> str:
        """Compile a plan to a SELECT statement. Values go to `params`, in the order of the `?` marks."""
        op = plan["op"]
        if op == "scan":
            if plan["format"] == "duckdb":
                return f"SELECT * FROM {self._attach(plan['path'])}.{_q(plan['table'])}"
            return f"SELECT * FROM read_{plan['format']}({_lit(plan['path'])})"
        if op == "stored":
            return f"SELECT * FROM {_q(plan['table'])}"
        if op == "join":
            left, right = self._sql(plan["left"], params), self._sql(plan["right"], params)
            cols = [f"l.{_q(c)}" for c in plan["left_columns"]] + [f"r.{_q(c)}" for c in plan["right_columns"]]
            on = " AND ".join(f"l.{_q(k['left'])} = r.{_q(k['right'])}" for k in plan["on"])
            return f"SELECT {', '.join(cols)} FROM ({left}) AS l {plan['how'].upper()} JOIN ({right}) AS r ON {on}"
        src = f"({self._sql(plan['input'], params)}) AS t"
        if op == "filter":
            terms = []
            for p in plan["where"]:
                if p["op"] == "in":
                    terms.append(f"{_q(p['column'])} IN ({', '.join('?' * len(p['value']))})")
                    params.extend(p["value"])
                else:
                    terms.append(f"{_q(p['column'])} {_OPS[p['op']]} ?")
                    params.append(p["value"])
            return f"SELECT * FROM {src} WHERE {' AND '.join(terms)}"
        if op == "select":
            return f"SELECT {', '.join(map(_q, plan['columns']))} FROM {src}"
        if op == "aggregate":
            keys = [_q(c) for c in plan["group_by"]]
            metrics = [f"{_FNS[m['fn']].format(_q(m['column']) if 'column' in m else '*')} AS {_q(m['as'])}" for m in plan["metrics"]]
            return f"SELECT {', '.join(keys + metrics)} FROM {src}" + (f" GROUP BY {', '.join(keys)}" if keys else "")
        if op == "sort":
            return f"SELECT * FROM {src} ORDER BY {', '.join(_q(b['column']) + (' DESC' if b.get('descending') else '') for b in plan['by'])}"
        if op == "limit":
            return f"SELECT * FROM {src} LIMIT {int(plan['n'])}"
        raise ValueError(op)

    def _run(self, plan: dict, template: str = "{}"):
        params: list = []
        return self.db.execute(template.format(self._sql(plan, params)), params)

    def _columns(self, plan: dict) -> list[dict]:
        return [{"name": r[0], "type": r[1]} for r in self._run(plan, "DESCRIBE {}").fetchall()]

    def _version(self, plan: dict) -> str:
        """The version of each source file under a plan: modification time and size."""
        if plan["op"] == "scan":
            st = os.stat(plan["path"])
            return f"{st.st_mtime_ns}:{st.st_size}"
        if plan["op"] == "stored":
            return plan["source_version"]
        parts = [self._version(plan[k]) for k in ("input", "left", "right") if k in plan]
        return "+".join(sorted(set(parts)))

    # ---- adapter contract ---------------------------------------------------------------------

    def open(self, source: dict) -> ResourceRef:
        path = Path(source["path"]).resolve(strict=True)
        fmt = {".parquet": "parquet", ".csv": "csv", ".duckdb": "duckdb", ".db": "duckdb"}.get(path.suffix.lower())
        if fmt is None:
            raise HandleError("INVALID_INPUT", f"no reader for {path.suffix!r} files")
        plan = {"op": "scan", "path": str(path), "format": fmt}
        if fmt == "duckdb":
            if "table" not in source:
                raise HandleError("INVALID_INPUT", "a DuckDB database source needs a table name")
            plan["table"] = source["table"]
        self._columns(plan)  # the source must be readable now
        return ResourceRef("dataset", {"plan": plan}, self._version(plan))

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "A table of rows and columns.", "schema": {"columns": self._columns(ref.locator["plan"])}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        return {"version": self._version(ref.locator["plan"])}

    def _check(self, plan: dict, names) -> list[str]:
        have = [c["name"] for c in self._columns(plan)]
        missing = [n for n in names if n not in have]
        if missing:
            raise HandleError("INVALID_INPUT", f"no such column: {missing}", details={"columns": have})
        return have

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        # A cancel interrupts the query that runs now. The adapter lock lets only this call use the connection.
        remove = call.on_cancel(self.db.interrupt)
        try:
            return self._invoke(ref, capability, input, eager)
        except duckdb.Error:
            call.check()  # an interrupted query becomes JOB_CANCELLED or OPERATION_TIMEOUT
            raise
        finally:
            remove()

    def _invoke(self, ref: ResourceRef, capability: str, input: dict, eager: bool) -> Value | Derived:
        plan, verb = ref.locator["plan"], capability.split(".")[1]
        if verb == "schema":
            return Value({"columns": self._columns(plan)}, self._version(plan))
        if verb == "count":
            return Value({"rows": self._run(plan, "SELECT count(*) FROM ({})").fetchone()[0]}, self._version(plan))
        if verb == "head":
            return self._head(plan, input.get("n", 10))
        if verb == "filter":
            self._check(plan, [p["column"] for p in input["where"]])
            for p in input["where"]:
                if isinstance(p["value"], list) != (p["op"] == "in"):
                    raise HandleError("INVALID_INPUT", f"op {p['op']!r} does not take this value: {p['value']!r}")
            new = {"op": "filter", "input": plan, "where": input["where"]}
        elif verb == "select":
            self._check(plan, input["columns"])
            new = {"op": "select", "input": plan, "columns": input["columns"]}
        elif verb == "aggregate":
            group_by = input.get("group_by", [])
            self._check(plan, [*group_by, *(m["column"] for m in input["metrics"] if "column" in m)])
            for m in input["metrics"]:
                if "column" not in m and m["fn"] != "count":
                    raise HandleError("INVALID_INPUT", f"metric {m['as']!r}: {m['fn']} needs a column")
            new = {"op": "aggregate", "input": plan, "group_by": group_by, "metrics": input["metrics"]}
        elif verb == "sort":
            self._check(plan, [b["column"] for b in input["by"]])
            new = {"op": "sort", "input": plan, "by": input["by"]}
        elif verb == "limit":
            new = {"op": "limit", "input": plan, "n": input["n"]}
        elif verb == "join":
            other = input["right"].locator["plan"]
            left = self._check(plan, [k["left"] for k in input["on"]])
            keys = [k["right"] for k in input["on"]]
            right = [c for c in self._check(other, keys) if c not in keys]  # the right keys equal the left keys
            both = sorted(set(left) & set(right))
            if both:
                raise HandleError("INVALID_INPUT", f"both sides have these columns: {both}. Use dataset.select on one side first.")
            new = {"op": "join", "left": plan, "right": other, "on": input["on"], "how": input.get("how", "inner"),
                   "left_columns": left, "right_columns": right}
        else:
            raise HandleError("CAPABILITY_NOT_FOUND", capability)
        if not eager:
            return Derived(ResourceRef("dataset", {"plan": new}))
        table, version = new_id("res"), self._version(new)
        self._run(new, f"CREATE TABLE {_q(table)} AS {{}}")
        stored = {"op": "stored", "table": table, "source_version": version}
        return Derived(ResourceRef("dataset", {"plan": stored}, version), kind="result", consistency="snapshot")

    def _head(self, plan: dict, n: int) -> Value:
        cursor = self._run(plan, f"SELECT * FROM ({{}}) LIMIT {int(n)}")
        names = [d[0] for d in cursor.description]
        budget = DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE - len(json.dumps(names).encode())
        rows, truncated = [], False
        while (row := cursor.fetchone()) is not None:
            row = json.loads(json.dumps(row, default=_plain))
            budget -= len(json.dumps(row).encode()) + 2
            if budget < 0:
                truncated = True
                break
            rows.append(row)
        return Value({"columns": names, "rows": rows, "truncated": truncated}, self._version(plan))

    def release(self, ref: ResourceRef, kind: str) -> None:
        plan = ref.locator["plan"]
        if plan["op"] == "stored":  # a table that this adapter made; a source file is never touched
            self.db.execute(f"DROP TABLE IF EXISTS {_q(plan['table'])}")
