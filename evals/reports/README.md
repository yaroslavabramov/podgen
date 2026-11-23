# Recorded evaluation results

## Laying the Atlantic telegraph cable

Could the writing pipeline tell a 30–33 minute documentary from a retained research corpus? The planner and its editor ran on the same materials in two trials. The first trial stopped during the initial factual reading after reaching the cumulative input-token limit. The second allowed a model to request several independent source reads in a single turn and used a USD budget.

| Measure | First trial | Second trial |
| --- | ---: | ---: |
| Model turns for planning and plan review | 103 | 24 |
| Input tokens for those stages | 2,874,346 | 851,246 |
| Writing outcome | Stopped during the first fact-check | Both reviewers accepted the first revision |

In the completed trial the factual reviewer found an attribution error: the draft gave the person who redesigned the cable-paying machinery credit for inventing its self-releasing brake. The revised script separated the two contributions. The listener's review removed several explanations that repeated actions the listener had just heard.

The complete writing cycle took 52 minutes and 57 generations. Of its 2,552,231 input tokens, 1,769,850 were cached. The ledger recorded $2.391449 for writing and review at the rates pinned to that run. The trial reused earlier research and stopped after writing. Its final 3,887 words were estimated at 28:41 for the chosen voice, below the 30-minute minimum. The reviewers accepted the script despite that estimate. There is no measured audio duration for this version.

In the opening scene, the *Niagara* is paying out cable from its stern. Here is the next paragraph in English translation:

> Men stand by the machinery, watching that the rope runs out neither too fast nor too slowly. Electricians sit nearby with their instruments in front of them. They send a current down the rope and wait for an answer from shore. The rope is a telegraph line. They mean to lay it across the Atlantic Ocean.

[Final Russian script](2026-11-23-atlantic-cost-budget/final.md) · [Token and cost ledger](2026-11-23-atlantic-cost-budget/accounting.json)

## From script to audio

An earlier, shorter Russian episode on Egyptian hieroglyphs completed research, writing and synthesis. The exported assembly contained 35,122,560 audio frames at 48 kHz, or 12:11.72. The assembly verified full script coverage. An acoustic review is a separate production decision.

The [Apollo 13 and rain–soil evaluations](2026-11-12-two-topic-cycles/README.md) retain the exact final decisions used by the shared audio fixtures. The adjacent Snow review records serve the same purpose.

## Reproducing historical work

Current cases and operator tools live in [the evaluations directory](../README.md). The development snapshot at `6b668a7c` contains the full collection of dated experiments, intermediate scripts and run notes. In a clone containing that history, use `git ls-tree --name-only 6b668a7c:evals/reports` to list them and `git show 6b668a7c:evals/reports/<experiment>/README.md` to inspect a report. Write new raw exports and provider responses under the ignored `tmp/` directory.
