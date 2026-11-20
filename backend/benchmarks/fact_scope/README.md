# Whole-script versus sectional fact reading

A controlled evaluation of a saved independent script revision. The two arms use
the same model profile, current factual instructions and admission rules, script,
and retained documents. They start as first readings. Neither receives old verdicts.
Source tools operate against the fixed inventory, including documents already
admitted by the source run. Open web acquisition is deliberately outside this
comparison.

The whole arm receives the full assignment. In the split arm the model marks
narrative section boundaries. Each reader receives one complete assignment and the
whole episode as context. It inherits relevant passages by exact surviving quotes.
Each arm can look up any of the original documents. A final reader receives the
whole episode, source-bound decisions, and open issues, and returns one review.
The final reader must account for each required issue. Exact anchors, evidence
receipts and editorial decisions are validated before a result can be published.

Run with the backend environment, the saved local reference-state file, the source
run UUID, and a local output directory. The reference-state file contains private
database configuration. Keep it out of reports.

    python backend/benchmarks/fact_scope/run.py --state STATE --source-run UUID --stage revise-2 --destination OUT preview

Use the same arguments with `whole` and `split` for the two paid readings.
`partition` runs just the split arm's first step. Repeating a completed step
reads its journal entry. A changed binding or uncertain payment stops execution.
Each arm shares the source command's token allowances across its steps. Measurements
include the partition and final reader, their exact request identities, access
checks and journal work. Standalone runs have per-task setup costs. Compare the
actual decisions with the exact spoken text and source passages before using the
results to choose a production strategy.
