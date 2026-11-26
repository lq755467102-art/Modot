# Finding the extra turns in an agent task

[简体中文](engineering-efficiency.md)

A final answer tells us when the user could stop waiting. An independent grade tells us what was delivered. We record both, alongside model requests, tool calls, token usage and cost. Those measures can move in opposite directions.

## A smaller first request led to more work

The user switched tasks in a long conversation and asked, “Can you look for bugs?” The original run put 97 historical tool results in the first request. Two subsequent runs kept the originals locally, with a permission-checked retrieval path available to the agent.

| DeepSeek Flash, one run per column | Original | First retest | Second retest |
| --- | ---: | ---: | ---: |
| Old tool results in first request | 97 | 0 | 0 |
| First input, tokens | 177,586 | 11,676 | 11,675 |
| Model requests / tool calls | 21 / 29 | 30 / 47 | 57 / 110 |
| Total input tokens | 4,133,108 | 2,084,122 | 5,355,307 |
| Input cache hit rate | 99.35% | 96.00% | 97.45% |
| Total output tokens, including reasoning | 14,548 | 27,148 | 44,982 |
| Estimated cost, CNY | 0.1673 | 0.2320 | 0.4210 |
| Seconds from submission to completion | 505.139 | 157.913 | 267.944 |

Both retests shrank the first input by 93.4%. Neither retest fetched an old operation. In the 57-request run, ten tool calls fetched results from the **current** task. Its final input grew to 181,011 tokens as new receipts kept reappearing. That run finished sooner than the original and cost more. Access settings, dependencies, Git state and cache warmth also differed. The open-ended bug hunt had no exhaustive answer key, so the data supports verification of individual findings and leaves overall bug-finding accuracy open.

## Trace a failed claim through the system

| Observation | Check | What to change if confirmed |
| --- | --- | --- |
| The model reports “tests passed” | Inspect the executed command, working directory, source revision, named cases and final receipt. Did the test runner actually start? Can a known-bad input fail the independent grader? | Fix the environment if the command could not start. Improve the tool receipt if it hid named failures. If the model saw failures and still claimed success, preserve that case to score its final answer. |
| The agent keeps rerunning a check | Compare the command arguments, source revision, permissions and preceding result. Inspect the actual next model request to see which parts were visible. | Restore a missing result through its original operation ID. Keep a live operation addressable. Repeat the check when input or permission changed. |
| The first request looks cheap, but the task spends more | Separate old history from newly produced receipts, and cache hits from uncached input. Sum every model request including retries. | Test one context or tool change at a time, with total cost, elapsed time, delivery and false claims as separate outcomes. |
| A tool says it read an image, but the answer misses it | Inspect the final multimodal request and match the delivered image to its originating action. Recheck access when the tool finishes. | Record whether the pixels reached the model and add a regression where access changes during the read. |

Freezing the repository, interpreter, dependencies and permissions makes these observations easier to attribute. We run a known failure and a known fix before grading the agent. The desktop memory test uses this method on a single deliberately stale generated file. [Same-task experiment and grading](benchmarks/matched-desktop-memory.md)

For an investigation in the codebase, follow a receipt from storage to the assembled request and provider call. Then compare that request with the final answer and the external grader. [Context assembly](../loop/context_manager.py) · [Tool output retention](../loop/tool_spill.py) · [Provider request observation](../loop/model_observation.py) · [Per-case scorecard](benchmarks/modot-engineering-scorecard.md)
