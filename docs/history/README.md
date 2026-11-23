# Design history

Podgen went through two earlier approaches to writing a podcast. The first built scripts from a detailed outline. The second gave a researcher more freedom to investigate. Both informed the current separation between acquiring evidence, choosing a story and testing the resulting script on a reader.

- [Comprehension problems in the early writing pipeline](pipeline_clarity_and_comprehension_plan_2026-04-09.md)
- [Why the research and editorial workflow changed](generation_strategy_review_2026-09-08.md)
- [Initial storage and editorial-corpus verification](2026-11-05-wp01-wp02-review.md)
- [Current production architecture](../podcast-production/README.md)

The previous application, including its frontend and migrations, is preserved at commit `71aa34a630d2e181cdc288b5a8ab39329c981c3d` in the development history. In a clone containing that history, inspect the snapshot in a separate directory with:

```sh
git worktree add --detach ../podgen-legacy 71aa34a630d2e181cdc288b5a8ab39329c981c3d
```
