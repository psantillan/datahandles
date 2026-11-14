# v0.3 acceptance: an agent edits and runs a graph over a sensor, a simulation, and an action

Made by `benchmarks/agent_v03.py` on 2026-11-14. Model `ornith-1.5:9b` on Ollama (one GTX 1080), temperature 0,
context window 8,192 tokens, through the MCP server. Raw data with every message: `agent_v03_results.json`.

## Task

The model gets the three MCP tools and no handle id. The runtime holds a live room (a sensor and a heater), a
simulation of the room, and a graph `plan` over both (`benchmarks/v03_fixture.py`). The agent may run and edit the
graph. It may not approve the heater action. Before the task, the graph ran one time for the agent.

> Record the last 20 readings of the room as a window and get their mean temperature. In the graph named plan, set the input of the node `start` to that mean temperature (graph.set), then run the new graph. Tell me the predicted temperature after 60 steps with the heater on, and whether the heater switch ran or waits for an approval.

## Checks

Each check comes from the tool calls of the run, not from the reply alone:

- `recorded_window`: the agent recorded a window and read its statistics.
- `set_start_to_mean`: it set the node `start` to the mean of that window (within 0.01).
- `reused_unchanged_node`: in its last graph run, the node `baseline` (which does not depend on `start`) was reused.
- `ran_changed_nodes`: in that run, `start`, `heat`, and `predict_on` ran.
- `action_waits`: in that run, the heater switch had the status `waiting`.
- `answer_prediction`: the reply has the predicted temperature, from the model of the simulation (within 0.05).
- `answer_says_waiting`: the reply says that the switch waits for an approval.

## Results

| Run | Window (seq) | Predicted temperature | Tool calls | Failed calls | Peak prompt tokens | Time | Checks |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | 0 to 19 | 26.528 | 12 | 0 | 6,066 | 53.2 s | 7 of 7 |
| 2 | 484 to 503 | 25.534 | 12 | 0 | 6,014 | 56.7 s | 7 of 7 |
| 3 | 455 to 474 | 25.536 | 11 | 1 | 5,996 | 52.4 s | 7 of 7 |
| 4 | 385 to 404 | 25.537 | 12 | 1 | 6,033 | 55.9 s | 7 of 7 |
| 5 | 429 to 448 | 25.539 | 15 | 2 | 6,509 | 67.9 s | 7 of 7 |

- 5 of 5 runs pass every check.
- The largest peak prompt is 6,509 tokens, with three handles and a graph of 12 nodes.

## Method notes

- The first computation of `ran_changed_nodes` read a node with no `reused` field as reused, so it marked each run as
  failed. The function was corrected and the checks were computed again from the saved runs (`agent_v03.py recheck`).
- The task asks for the last 20 readings. No check tests "last": the window column shows which readings each run used.
  Run 1 recorded readings 0 to 19, the first 20 of the room, not the last 20.
- Ollama with temperature 0 and a fixed seed does not give the same tool calls in each run.
