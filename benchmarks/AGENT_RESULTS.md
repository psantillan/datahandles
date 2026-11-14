# Agent context: measured results

Made by `benchmarks/agent_context.py` on 2026-11-14. Model `ornith-1.5:9b` on Ollama, temperature 0, thinking off, context window 8,192 tokens. One run for each cell. DuckDB 2.0.0, Python 3.12.3.

The model gets two tools (`handle_describe`, `handle_invoke`) and two handle ids. Each cell: peak prompt tokens / tool calls (failed calls) / answer check.

| Question | Operations needed | 10,000 rows | 1,000,000 rows | 100,000,000 rows |
|---|---:|---|---|---|
| count | 1 | 1,988 tok / 2 (0) / correct | 1,998 tok / 2 (0) / correct | 2,030 tok / 2 (0) / correct |
| filter | 2 | 2,202 tok / 3 (0) / correct | 2,212 tok / 3 (0) / correct | 2,240 tok / 3 (0) / correct |
| group | 4 | 3,827 tok / 6 (0) / correct | 3,924 tok / 6 (0) / correct | 2,862 tok / 13 (9) / correct |
| join | 5 | 4,802 tok / 10 (0) / correct | 5,015 tok / 12 (3) / correct | 4,569 tok / 9 (0) / correct |

For comparison: one event row as JSON costs 35.4 prompt tokens with this model (measured on 30 rows). The rows of each dataset as JSON would cost about 353,667 tokens (10,000 rows), 35,366,667 tokens (1,000,000 rows), 3,536,666,667 tokens (100,000,000 rows) (calculated from the per-row cost). The context window of this run is 8,192 tokens.

Runs with a failed tool call:

- **join**, 1,000,000 rows: 3 `HANDLE_NOT_FOUND`. The model made 2 `handle_describe` calls. Its final answer was correct.
- **group**, 100,000,000 rows: 2 `CAPABILITY_NOT_FOUND`, 7 `INVALID_INPUT`. The model made 0 `handle_describe` calls. Its final answer was correct.

Questions:

- **count**: How many rows does the events dataset have?
- **filter**: How many events have the event_type 'purchase'?
- **group**: Which event_type has the largest total amount? Give the event_type.
- **join**: Which 3 countries have the most revenue from 'purchase' events? Revenue is the sum of amount. The country of a customer is in the customers dataset. Give the 3 country codes, largest first.

- Peak prompt tokens = the largest `prompt_eval_count` that the server reports in one conversation: system prompt, tool definitions, and all earlier turns.
- The answer check: the expected values, from one direct DuckDB SQL statement, must be in the final reply of the model, in order.
- `agent_results.json` has the prompt tokens of each turn. `agent_transcripts.json` has every message of every run.
