"""Let a local language model do one task over two kinds of resource through the MCP server.

The model gets the three MCP tools of `datahandles.protocols.mcp` and no handle id. The server holds
a git repository handle and a browser session handle (benchmarks/task_fixture.py). The answer is
correct only if it names the version from the web site, not the older version in the repository.

Needs an Ollama server with a model that supports tool calls.
Run: OLLAMA_URL=http://localhost:11434 OLLAMA_MODEL=some-model .venv/bin/python benchmarks/agent_mcp.py [runs]
"""
import json
import os
import platform
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_fixture  # noqa: E402

HERE = Path(__file__).resolve().parent
URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "")
NUM_CTX = 8192   # the context window that the run asks the server for, in tokens
MAX_TURNS = 16   # ASSUMED: a stop for a model that loops. The task needs 7 turns when no call fails.
SYSTEM = """You do tasks with resources that you cannot see directly. You have handles: references to resources.
Use handle_find to list your handles. Use handle_describe to learn what a handle can do and what each capability needs as input.
Use handle_invoke to run a capability.
When you have the answer, reply with the answer in one short sentence."""


def chat(messages: list, tools: list) -> dict:
    body = {"model": MODEL, "messages": messages, "tools": tools, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX}}
    request = urllib.request.Request(URL + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


async def run(work: Path, site: str) -> dict:
    repo = task_fixture.make_repo(work / "repo", site)
    command = StdioServerParameters(command=sys.executable, args=[
        "-m", "datahandles.protocols.mcp", "--state", str(work / "state"),
        "--register", f"repo=git:{repo}", "--register", "web=browser:"])
    async with Client(command) as client:
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                 for t in (await client.list_tools()).tools]
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task_fixture.QUESTION}]
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
                out = result.structured_content
                capability = fn["arguments"].get("capability")
                calls.append({"tool": fn["name"], "capability": capability, "failed": result.is_error})
                messages.append({"role": "tool", "tool_name": fn["name"], "content": json.dumps(out)})
    answer = message.get("content", "").strip()
    # The answer check: the reply names the newest version. (The first run also required that the reply
    # not name the README version. The model named it only to say that it is older, so that rule was dropped.)
    return {"answer": answer, "correct": task_fixture.ANSWER in answer,
            "used_git": any((c["capability"] or "").startswith("repo.") and not c["failed"] for c in calls),
            "used_browser": any((c["capability"] or "").startswith("browser.") and not c["failed"] for c in calls),
            "tool_calls": calls, "peak_prompt_tokens": max(prompt_tokens), "prompt_tokens_by_turn": prompt_tokens,
            "seconds": round(time.perf_counter() - t0, 1), "messages": messages}


def main(runs: int) -> None:
    assert MODEL, "set OLLAMA_MODEL"
    server, site = task_fixture.serve_site()
    results = []
    try:
        for _ in range(runs):  # each run starts with a new repository, a new state directory, and a new MCP server
            with tempfile.TemporaryDirectory() as work:
                result = anyio.run(run, Path(work), site)
            results.append(result)
            calls = ", ".join(f"{c['capability'] or c['tool']}{' (failed)' if c['failed'] else ''}" for c in result["tool_calls"])
            print(f"correct={result['correct']} git={result['used_git']} browser={result['used_browser']} "
                  f"peak={result['peak_prompt_tokens']} calls={len(result['tool_calls'])} [{calls}]", flush=True)
    finally:
        server.shutdown()
    out = {"date": time.strftime("%Y-%m-%d"), "model": MODEL, "num_ctx": NUM_CTX, "python": platform.python_version(), "runs": results}
    (HERE / "agent_mcp_results.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
