# Agent task over two kinds of resource, through MCP

Made by `benchmarks/agent_mcp.py` on 2026-11-14. Model `ornith-1.5:9b` on Ollama (one GTX 1080), temperature 0,
context window 8,192 tokens. Raw data with every message: `agent_mcp_results.json`.

## Task

The model gets the three tools of the MCP server (`handle_find`, `handle_describe`, `handle_invoke`) and no handle id.
The server holds a git repository handle and a browser session handle (`benchmarks/task_fixture.py`).

> What is the newest released version of the shop service? The repository tells you where its status page is. The status page links to the release notes. Give the version number.

The repository says where the status page is. The release notes on that site list the newest version, 3.4.1.
The README names an older version, 3.3.0, so an answer from the repository alone is wrong.

## Results

5 runs. Each run starts a new MCP server, a new state directory, and a new repository.

| Run | Tool calls | Git calls | Browser calls | Failed calls | Peak prompt tokens | Time | Answer |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 12 | 4 | 4 | 2 | 3,179 | 47.3 s | correct |
| 2 | 12 | 4 | 4 | 2 | 3,187 | 44.7 s | correct |
| 3 | 12 | 4 | 4 | 2 | 3,236 | 48.2 s | correct |
| 4 | 12 | 4 | 4 | 2 | 3,257 | 47.3 s | correct |
| 5 | 12 | 4 | 4 | 2 | 3,185 | 44.6 s | correct |

- 5 of 5 answers name 3.4.1. 5 of 5 runs used both handles.
- The peak prompt is 3,179 to 3,257 tokens. `handle_describe` gives the brief form (see `DESCRIBE_COMPARISON.md`).
- No code in the server, the runtime, or the tools knows that one handle is a repository and the other is a browser.

## Method notes

- The answer check: the final reply contains "3.4.1". (The first run of this benchmark, in v0.2, also required that
  the reply not contain "3.3.0". The model named it only to say that it is older, so that rule was dropped.)
- A failed call is a tool result with an error. The model corrects it from the error and goes on.
- Ollama with temperature 0 and a fixed seed does not give the same tool calls in each run.
