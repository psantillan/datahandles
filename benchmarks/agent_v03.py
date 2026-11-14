"""Give the v0.3 acceptance task to a local language model through the MCP server.

The model gets the three MCP tools and no handle id. It must record a window of a live room, read
its mean, edit one node of a graph to start a simulation from that mean, run the graph again, and
report the prediction and the state of the heater action (benchmarks/v03_fixture.py).

Needs an Ollama server with a model that supports tool calls.
Run: OLLAMA_URL=http://localhost:11434 OLLAMA_MODEL=some-model .venv/bin/python benchmarks/agent_v03.py [runs]
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
        "ran_changed_nodes": bool(last) and not any(last.get(n, {}).get("metadata", {}).get("reused", True) for n in ("start", "heat", "predict_on")),
        "action_waits": bool(last) and last.get("switch", {}).get("status") == "waiting",
        "answer_prediction": expected is not None and any(abs(n - expected) < 0.05 for n in numbers),
        "answer_says_waiting": bool(re.search(r"wait|approv", r["answer"], re.I)),
        "expected": expected,
    }


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


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
