"""Give the v0.3 acceptance task to a local language model through the MCP server.

The model gets the three MCP tools and no handle id. It must record a window of a live room, read
its mean, edit one node of a graph to start a simulation from that mean, run the graph again, and
report the prediction and the state of the heater action (benchmarks/v03_fixture.py).

Needs an Ollama server with a model that supports tool calls.
Run: OLLAMA_URL=http://localhost:11434 OLLAMA_MODEL=some-model .venv/bin/python benchmarks/agent_v03.py [runs]
Compute the checks again from the saved runs: .venv/bin/python benchmarks/agent_v03.py recheck
"""
import json
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v03_fixture  # noqa: E402
from agent_mcp import NUM_CTX, SYSTEM, chat  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("OLLAMA_MODEL", "")
MAX_TURNS = 24  # ASSUMED: a stop for a model that loops. The task needs about 10 turns when no call fails.


async def run(work: Path) -> dict:
    runtime, server, url, token, _ = v03_fixture.start(work / "state")
    try:
        command = StdioServerParameters(command=sys.executable, args=["-m", "datahandles.protocols.mcp", "--url", url],
                                        env={**os.environ, "DATAHANDLES_TOKEN": token})
        async with Client(command) as client:
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                     for t in (await client.list_tools()).tools]
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": v03_fixture.TASK}]
            calls, prompt_tokens, t0 = [], [], time.perf_counter()
            for _ in range(MAX_TURNS):
                reply = await anyio.to_thread.run_sync(chat, messages, tools)
                prompt_tokens.append(reply["prompt_eval_count"])
                message = reply["message"]
                messages.append(message)
                if not message.get("tool_calls"):
                    break
                for call in message["tool_calls"]:
                    fn = call["function"]
                    result = await client.call_tool(fn["name"], fn["arguments"])
                    calls.append({"tool": fn["name"], "arguments": fn["arguments"], "failed": result.is_error, "output": result.structured_content})
                    messages.append({"role": "tool", "tool_name": fn["name"], "content": json.dumps(result.structured_content)})
    finally:
        server.shutdown()
        server.server_close()
    return {"answer": message.get("content", "").strip(), "calls": calls, "messages": messages,
            "peak_prompt_tokens": max(prompt_tokens), "seconds": round(time.perf_counter() - t0, 1)}


def check(r: dict) -> dict:
    """What the run did, from its tool calls. Each item is true or false."""
    ok = [c for c in r["calls"] if not c["failed"] and c["tool"] == "handle_invoke"]
    by = {}
    for c in ok:
        by.setdefault(c["arguments"].get("capability"), []).append(c)
    mean = by.get("window.stats", [{}])[-1].get("output", {}).get("output", {}).get("mean")
    sets = [c["arguments"]["input"] for c in by.get("graph.set", [])]
    runs = [c["output"]["output"]["results"] for c in by.get("graph.run", [])]
    last = runs[-1] if runs else {}
    expected = v03_fixture.expected(mean) if mean is not None else None
    numbers = [float(x) for x in re.findall(r"-?\d+\.\d+", r["answer"])]
    return {
        "recorded_window": "room.record" in by and mean is not None,
        "set_start_to_mean": any(s.get("node") == "start" and abs(s.get("input", {}).get("temperature", 1e9) - (mean or 0)) < 0.01 for s in sets),
        "reused_unchanged_node": bool(last) and last.get("baseline", {}).get("metadata", {}).get("reused", False),
        "ran_changed_nodes": bool(last) and all(last.get(n, {}).get("status") == "completed" and not last[n]["metadata"].get("reused", False)
                                                for n in ("start", "heat", "predict_on")),
        "action_waits": bool(last) and last.get("switch", {}).get("status") == "waiting",
        "answer_prediction": expected is not None and any(abs(n - expected) < 0.05 for n in numbers),
        "answer_says_waiting": bool(re.search(r"wait|approv", r["answer"], re.I)),
        "expected": expected,
    }


def recheck() -> None:
    """Compute the checks again from the saved runs. No model is needed."""
    path = HERE / "agent_v03_results.json"
    out = json.loads(path.read_text())
    for r in out["runs"]:
        r["check"] = check(r)
        r["correct"] = all(v for k, v in r["check"].items() if k != "expected")
    path.write_text(json.dumps(out, indent=1) + "\n")
    print([r["correct"] for r in out["runs"]])


def main(runs: int) -> None:
    assert MODEL, "set OLLAMA_MODEL"
    results = []
    for _ in range(runs):
        with tempfile.TemporaryDirectory() as work:
            r = anyio.run(run, Path(work))
        r["check"] = check(r)
        r["correct"] = all(v for k, v in r["check"].items() if k != "expected")
        results.append(r)
        steps = ", ".join(f"{c['arguments'].get('capability') or c['tool']}{' (failed)' if c['failed'] else ''}" for c in r["calls"])
        print(f"correct={r['correct']} peak={r['peak_prompt_tokens']} calls={len(r['calls'])} {r['check']} [{steps}]", flush=True)
    out = {"date": time.strftime("%Y-%m-%d"), "model": MODEL, "num_ctx": NUM_CTX, "python": platform.python_version(), "runs": results}
    (HERE / "agent_v03_results.json").write_text(json.dumps(out, indent=1) + "\n")


def report() -> None:
    """Write AGENT_V03_RESULTS.md from agent_v03_results.json. This step needs no model."""
    d = json.loads((HERE / "agent_v03_results.json").read_text())
    runs = d["runs"]
    names = [k for k in runs[0]["check"] if k != "expected"]
    rows = []
    for i, r in enumerate(runs, 1):
        stats = [c["output"]["output"] for c in r["calls"] if c["tool"] == "handle_invoke" and not c["failed"]
                 and c["arguments"].get("capability") == "window.stats"]
        window = f"{stats[-1]['first_seq']} to {stats[-1]['last_seq']}" if stats else "-"
        rows.append(f"| {i} | {window} | {r['check']['expected']} | {len(r['calls'])} | {sum(c['failed'] for c in r['calls'])} | "
                    f"{r['peak_prompt_tokens']:,} | {r['seconds']} s | {sum(r['check'][n] for n in names)} of {len(names)} |")
    lines = ["# v0.3 acceptance: an agent edits and runs a graph over a sensor, a simulation, and an action", "",
             f"Made by `benchmarks/agent_v03.py` on {d['date']}. Model `{d['model']}` on Ollama (one GTX 1080), temperature 0,",
             f"context window {d['num_ctx']:,} tokens, through the MCP server. Raw data with every message: `agent_v03_results.json`.", "",
             "## Task", "",
             "The model gets the three MCP tools and no handle id. The runtime holds a live room (a sensor and a heater), a",
             "simulation of the room, and a graph `plan` over both (`benchmarks/v03_fixture.py`). The agent may run and edit the",
             "graph. It may not approve the heater action. Before the task, the graph ran one time for the agent.", "",
             f"> {v03_fixture.TASK}", "",
             "## Checks", "",
             "Each check comes from the tool calls of the run, not from the reply alone:", "",
             "- `recorded_window`: the agent recorded a window and read its statistics.",
             "- `set_start_to_mean`: it set the node `start` to the mean of that window (within 0.01).",
             "- `reused_unchanged_node`: in its last graph run, the node `baseline` (which does not depend on `start`) was reused.",
             "- `ran_changed_nodes`: in that run, `start`, `heat`, and `predict_on` ran.",
             "- `action_waits`: in that run, the heater switch had the status `waiting`.",
             "- `answer_prediction`: the reply has the predicted temperature, from the model of the simulation (within 0.05).",
             "- `answer_says_waiting`: the reply says that the switch waits for an approval.", "",
             "## Results", "",
             "| Run | Window (seq) | Predicted temperature | Tool calls | Failed calls | Peak prompt tokens | Time | Checks |",
             "|---:|---|---:|---:|---:|---:|---:|---|", *rows, "",
             f"- {sum(r['correct'] for r in runs)} of {len(runs)} runs pass every check.",
             f"- The largest peak prompt is {max(r['peak_prompt_tokens'] for r in runs):,} tokens, with three handles and a graph of 12 nodes.", "",
             "## Method notes", "",
             "- The first computation of `ran_changed_nodes` read a node with no `reused` field as reused, so it marked each run as",
             "  failed. The function was corrected and the checks were computed again from the saved runs (`agent_v03.py recheck`).",
             "- The task asks for the last 20 readings. No check tests \"last\": the window column shows which readings each run used.",
             "  Run 1 recorded readings 0 to 19, the first 20 of the room, not the last 20.",
             "- Ollama with temperature 0 and a fixed seed does not give the same tool calls in each run."]
    (HERE / "AGENT_V03_RESULTS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    commands = {"recheck": recheck, "report": report}
    commands[sys.argv[1]]() if sys.argv[1:2] and sys.argv[1] in commands else main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
