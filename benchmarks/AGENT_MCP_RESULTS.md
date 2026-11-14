# Agent task over two kinds of resource, through MCP

Made by `benchmarks/agent_mcp.py` on 2026-11-14. Model `ornith-1.5:9b` on Ollama (one GTX 1080), temperature 0,
context window 8,192 tokens. Raw data with every message: `agent_mcp_results.json`.

## Task

The model gets the three tools of the MCP server (`handle_find`, `handle_describe`, `handle_invoke`)
and no handle id. The server holds two handles: a git repository (`git` adapter) and a browser
session (`browser` adapter). The fixture is `benchmarks/task_fixture.py`.

> What is the newest released version of the shop service? The repository tells you where its status page is. The status page links to the release notes. Give the version number.

The repository says where the status page is (in `docs/OPERATIONS.md`, which the README points to).
The status page links to the release notes, and the release notes list the newest version, 3.4.1.
The README names an older version, 3.3.0, so an answer from the repository alone is wrong.

## Results

5 runs. Each run starts a new MCP server, a new state directory, and a new repository.

| Run | Tool calls | Git calls | Browser calls | Failed calls | Peak prompt tokens | Time | Answer |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 12 | 4 | 5 | 0 | 3,868 | 42.4 s | correct |
| 2 | 14 | 6 | 5 | 0 | 4,200 | 53.2 s | correct |
| 3 | 12 | 4 | 5 | 0 | 3,906 | 45.1 s | correct |
| 4 | 15 | 8 | 4 | 0 | 4,536 | 68.6 s | correct |
| 5 | 11 | 3 | 5 | 0 | 3,570 | 44.0 s | correct |

- 5 of 5 answers name 3.4.1. Every run used both handles. No tool call failed.
- 4 of 5 answers also name 3.3.0, each time to say that the README version is older.
- The peak prompt stays under 4,536 tokens with two handles and up to 15 tool calls.
- No code in the server, the runtime, or the tools knows that one handle is a repository and the
  other is a browser. The model learns each from `handle_describe`.

## Method notes

- The answer check: the final reply contains "3.4.1".
- Two runs came before these five, while the harness was built. The first used a stricter check that
  also required the reply not to contain "3.3.0". Its reply was "the newest released version is 3.4.1"
  and it named 3.3.0 only as the older README version, so the check marked a correct answer as wrong.
  That rule was dropped. Both early runs named 3.4.1.
- Ollama with temperature 0 and a fixed seed did not give the same tool calls in each run, so the
  runs differ.
- The web site and the MCP server run on the same machine. The model runs on a second machine.
