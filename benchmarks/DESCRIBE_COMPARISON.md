# Brief handle.describe: measured

Made from the saved runs of `agent_context.py` (dataset benchmark: 4 questions x 3 sizes, one run for each cell) and
`agent_mcp.py` (two-resource task, 5 runs). Model `ornith-1.5:9b`, temperature 0, context window 8,192 tokens. Same questions,
same prompts, same model in each condition. The raw runs are in this folder and in `agent_results.json` / `agent_mcp_results.json`.

| Condition | Dataset: correct | Dataset: median peak prompt | Dataset: failed tool calls | Two-resource: correct | Two-resource: peak prompt |
|---|---:|---:|---:|---:|---|
| full describe (v0.2) | 12 of 12 | 2,551 | 12 | 5 of 5 | 3,570 to 4,536 |
| brief describe | 8 of 12 | 2,443 | 62 | 5 of 5 | 3,062 to 4,044 |
| brief describe + schema in the error (v0.3) | 11 of 12 | 2,216 | 26 | 5 of 5 | 3,179 to 3,257 |

What the numbers say:

- The brief form lowers the peak prompt. With the schema in the error, the peak is lower than v0.2 in 11 of 12 dataset cells and in each two-resource run.
- The brief form alone lost accuracy (8 of 12). The model guessed input shapes and corrected one field for each error. All three join
  runs reached the limit of 16 turns before an answer, and one group run gave a wrong answer.
  An input error now carries the input schema of the capability, and the model corrects the whole input in one step.
- With both changes the dataset benchmark is 11 of 12, against 12 of 12 with the full describe. The wrong run (join, 10,000 rows) had no
  schema problem left: the model joined the unfiltered events table, so it summed every event type. With one run for each cell,
  this does not show that the accuracy is equal or that it is lower.
- The two-resource task is 5 of 5 in each condition.
