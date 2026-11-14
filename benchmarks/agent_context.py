"""Let a local language model answer questions of growing complexity on datasets of growing size.

The model gets two tools (packages/protocols/tools.py) and two handle ids. It never gets a file.
For each (question, size) this script records the peak prompt size in tokens, as the model
server counts it, and checks the final answer against a direct DuckDB SQL statement.

Needs an Ollama server with a model that supports tool calls.
Run: OLLAMA_URL=http://localhost:11434 OLLAMA_MODEL=some-model .venv/bin/python benchmarks/agent_context.py [max_rows]
Write the report again from the saved JSON files: .venv/bin/python benchmarks/agent_context.py report
"""
import json
import os
import platform
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import duckdb

from datahandles import Client, connect
from datahandles.protocols.tools import TOOLS, dispatch

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "")
NUM_CTX = 8192   # the context window that the run asks the server for, in tokens
MAX_TURNS = 16   # ASSUMED: a stop for a model that loops. The longest question needs 7 turns when no call fails.

EVENTS = """SELECT i AS id, (hash(i) % 100000)::BIGINT AS customer_id, ['purchase', 'view', 'click'][(1 + hash(i + 1) % 3)::BIGINT] AS event_type,
            (hash(i + 2) % 20000)::BIGINT / 100.0 AS amount FROM range({n}) t(i)"""
CUSTOMERS = "SELECT i AS customer_id, ['US', 'MX', 'CA', 'BR', 'DE', 'JP', 'IN'][(1 + hash(i) % 7)::BIGINT] AS country FROM range(100000) t(i)"
SYSTEM = """You answer questions about data that you cannot see directly. You have handles: references to resources.
Use handle_describe to learn what a handle is and what its capabilities need as input.
Use handle_invoke to run a capability. Some capabilities return a new handle. Use that new handle id in your next call.
To pass a handle as an input value, write {{"$handle": "<handle id>"}}.
When you have the answer, reply with the answer in one short sentence. Give numbers as plain digits.

The id of the events handle is {events}
The id of the customers handle is {customers}"""

# (name, question, operations needed, SQL that gives the expected answer)
QUESTIONS = [
    ("count", "How many rows does the events dataset have?", 1,
     "SELECT count(*) FROM '{e}'"),
    ("filter", "How many events have the event_type 'purchase'?", 2,
     "SELECT count(*) FROM '{e}' WHERE event_type = 'purchase'"),
    ("group", "Which event_type has the largest total amount? Give the event_type.", 4,
     "SELECT event_type FROM '{e}' GROUP BY 1 ORDER BY sum(amount) DESC LIMIT 1"),
    ("join", "Which 3 countries have the most revenue from 'purchase' events? Revenue is the sum of amount. The country of a customer is in the customers dataset. Give the 3 country codes, largest first.", 5,
     "SELECT country FROM '{e}' JOIN '{c}' USING (customer_id) WHERE event_type = 'purchase' GROUP BY 1 ORDER BY sum(amount) DESC LIMIT 3"),
]


def chat(messages: list, tools: list = TOOLS, **options) -> dict:
    body = {"model": MODEL, "messages": messages, "tools": tools, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX, **options}}
    request = urllib.request.Request(URL + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)


def run_agent(agent: Client, system: str, question: str) -> dict:
    """One conversation. Return the final text and the measurements."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    calls = errors = tool_bytes = 0
    prompt_tokens = []
    t0 = time.perf_counter()
    for _ in range(MAX_TURNS):
        reply = chat(messages)
        prompt_tokens.append(reply["prompt_eval_count"])
        message = reply["message"]
        messages.append(message)
        if not message.get("tool_calls"):
            break
        for call in message["tool_calls"]:
            fn = call["function"]
            result = dispatch(agent, fn["name"], fn["arguments"])
            text = json.dumps(result)
            calls, errors, tool_bytes = calls + 1, errors + ("error" in result), tool_bytes + len(text.encode())
            messages.append({"role": "tool", "tool_name": fn["name"], "content": text})
    return {"answer": message.get("content", "").strip(), "peak_prompt_tokens": max(prompt_tokens), "prompt_tokens_by_turn": prompt_tokens, "tool_calls": calls,
            "tool_errors": errors, "tool_result_bytes": tool_bytes, "seconds": round(time.perf_counter() - t0, 1), "messages": messages}


def tokens_per_row(path: Path) -> float:
    """Measure what one event row costs in prompt tokens when the rows go to the model as JSON."""
    def count(n: int) -> int:
        table = duckdb.sql(f"SELECT * FROM '{path}' LIMIT {n}")
        rows = [dict(zip(table.columns, r)) for r in table.fetchall()]
        return chat([{"role": "user", "content": json.dumps(rows)}], tools=[], num_predict=1)["prompt_eval_count"]
    return (count(60) - count(30)) / 30  # the difference removes the fixed cost of the chat template


def correct(answer: str, expected: list) -> bool:
    """The expected values must be in the answer text, in order. Digit group separators are ignored."""
    text, position = re.sub(r"(?<=\d)[,_ ](?=\d{3})", "", answer), 0
    for value in expected:
        found = re.search(rf"(?<![\w.]){re.escape(str(value))}(?![\w]|\.\d)", text[position:])
        if not found:
            return False
        position += found.end()
    return True


def main(max_rows: int) -> None:
    assert MODEL, "set OLLAMA_MODEL"
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir()
    duckdb.sql("SET memory_limit = '4GB'")  # for the data generator; the limit is far above its need
    owner = connect(DATA / "state", principal="owner")
    agent = Client(owner.runtime, "agent")
    customers_path = DATA / "customers.parquet"
    duckdb.sql(f"COPY ({CUSTOMERS}) TO '{customers_path}'")
    customers = owner.register("customers", adapter="duckdb", source=str(customers_path))
    customers.grant("agent", ["*"])
    rows, transcripts, n, per_row = [], {}, 10_000, 0.0
    while n <= max_rows:
        path = DATA / f"events_{n}.parquet"
        duckdb.sql(f"COPY ({EVENTS.format(n=n)}) TO '{path}'")
        events = owner.register(f"events_{n}", adapter="duckdb", source=str(path))
        events.grant("agent", ["*"])
        per_row = per_row or tokens_per_row(path)
        for name, question, operations, sql in QUESTIONS:
            expected = [r[0] for r in duckdb.sql(sql.format(e=path, c=customers_path)).fetchall()]
            run = run_agent(agent, SYSTEM.format(events=events.id, customers=customers.id), question)
            transcripts[f"{name}_{n}"] = run.pop("messages")
            rows.append({"question": name, "operations": operations, "rows": n, "parquet_bytes": path.stat().st_size,
                         "expected": expected, "correct": correct(run["answer"], expected), **run})
            print(json.dumps(rows[-1]), flush=True)
        path.unlink()
        n *= 100
    (HERE / "agent_results.json").write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d"), "model": MODEL, "num_ctx": NUM_CTX, "duckdb": duckdb.__version__,
        "python": platform.python_version(), "tokens_per_row_as_json": per_row, "runs": rows}, indent=2) + "\n")
    (HERE / "agent_transcripts.json").write_text(json.dumps(transcripts, indent=1) + "\n")
    shutil.rmtree(DATA)
    report()


def report() -> None:
    """Write AGENT_RESULTS.md from the two JSON files of the last run. This step needs no model."""
    data = json.loads((HERE / "agent_results.json").read_text())
    transcripts = json.loads((HERE / "agent_transcripts.json").read_text())
    rows, per_row = data["runs"], data["tokens_per_row_as_json"]
    sizes = sorted({r["rows"] for r in rows})
    cell = {(r["question"], r["rows"]): r for r in rows}
    lines = [
        "# Agent context: measured results", "",
        f"Made by `benchmarks/agent_context.py` on {data['date']}. Model `{data['model']}` on Ollama, temperature 0, "
        f"thinking off, context window {data['num_ctx']:,} tokens. One run for each cell. DuckDB {data['duckdb']}, Python {data['python']}.", "",
        "The model gets two tools (`handle_describe`, `handle_invoke`) and two handle ids. Each cell: peak prompt tokens / tool calls (failed calls) / answer check.", "",
        "| Question | Operations needed | " + " | ".join(f"{s:,} rows" for s in sizes) + " |", "|---|---:|" + "---|" * len(sizes),
    ]
    for name, question, operations, _ in QUESTIONS:
        cells = [f"{c['peak_prompt_tokens']:,} tok / {c['tool_calls']} ({c['tool_errors']}) / {'correct' if c['correct'] else 'WRONG'}"
                 for c in (cell[(name, s)] for s in sizes)]
        lines.append(f"| {name} | {operations} | " + " | ".join(cells) + " |")
    lines += ["", f"For comparison: one event row as JSON costs {per_row:.1f} prompt tokens with this model (measured on 30 rows). "
              "The rows of each dataset as JSON would cost about " + ", ".join(f"{s * per_row:,.0f} tokens ({s:,} rows)" for s in sizes)
              + f" (calculated from the per-row cost). The context window of this run is {data['num_ctx']:,} tokens."]
    lines += ["", "Runs with a failed tool call:", ""]
    for r in rows:
        messages = transcripts[f"{r['question']}_{r['rows']}"]
        codes = [json.loads(m["content"])["error"]["code"] for m in messages if m["role"] == "tool" and m["content"].startswith('{"error"')]
        describes = sum(c["function"]["name"] == "handle_describe" for m in messages for c in m.get("tool_calls") or [])
        if codes:
            counts = ", ".join(f"{codes.count(c)} `{c}`" for c in sorted(set(codes)))
            lines.append(f"- **{r['question']}**, {r['rows']:,} rows: {counts}. The model made {describes} `handle_describe` calls. Its final answer was {'correct' if r['correct'] else 'WRONG'}.")
    lines += ["", "Questions:", ""] + [f"- **{name}**: {question}" for name, question, _, _ in QUESTIONS]
    lines += ["", "- Peak prompt tokens = the largest `prompt_eval_count` that the server reports in one conversation: system prompt, tool definitions, and all earlier turns.",
              "- The answer check: the expected values, from one direct DuckDB SQL statement, must be in the final reply of the model, in order.",
              "- `agent_results.json` has the prompt tokens of each turn. `agent_transcripts.json` has every message of every run.", ""]
    (HERE / "AGENT_RESULTS.md").write_text("\n".join(lines))


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        report()
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000_000)
