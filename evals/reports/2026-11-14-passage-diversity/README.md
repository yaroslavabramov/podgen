# Passage diversity qualification

Policy: `passage-diversity/1`, for both hybrid-local/1 and hybrid-local/2.

Exact passage SHA-256 groups are selected before each SQL branch cap. The
post-ranking operation additionally groups overlapping windows and close textual
versions. Versions are navigation groups, never assertions of factual equivalence
or publication lineage. Every edit is shown verbatim on both sides, with exact
source coordinates and original context. Full original window selectors survive;
all differences must fit the 1024-byte budget or the hits remain separate.

## Saved-ranking qualification

Input: 25 complete `source_search_diagnostics.ranked` arrays from the saved
longitude run `f7e9dbac-c64c-5698-807a-fa8b25e69a2f`. Source text is loaded from
the frozen original corpus. This check uses production grouping and preview code,
without SQL candidate selection, ONNX inference, provider calls or index writes.
The original order and scores are inputs; they are not recomputed.

Result: 25 queries in 5.19 seconds. Two old top-six slots are freed, one each in
queries 11 and 16. Both returned pages still contain six hits. Other queries keep
their useful independent windows, including the other four queries containing
S44/S50 in the first six results.

Both changed queries group original windows S44 `2306:3278` and S50
`2312:3248` under expanded S44 `2215:3278`. The visible difference is:

- S44 `2631:2631`: empty; context `2609:2669`, “to start working on a pocket
  watch-sized H4. That would take”.
- S50 `2729:2736`: `(large)`; context `2707:2770`, “to start working on a
  (large) pocket watch-sized H4. That would”.

The newly visible sixth hit is S27 `4065:5085` in query 11: Jamaica trial,
Madeira landfall, the Commissioners' dissatisfaction and the subsequent dispute.
In query 16 it is S49 `5381:6237`: Kendall's payment, K1's Cook trials and the
commission of the simpler K2. Complete merged text, original windows, differences
and newly visible passages are in `grouping-qualification.json`.

Reproduce from the repository root:

```sh
backend/.venv/bin/python backend/benchmarks/passage_diversity/regroup.py \
  --state tmp/longitude-corpus-2026-11-13/state.json \
  --shared-ref tmp/longitude-corpus-2026-11-13/shared-ref.json \
  --review /tmp/podgen-longitude-tool-review-f7e9.json \
  --out /tmp/podgen-passage-regroup-20261114
```

The state file contains private connection details and is not part of this report.

## Validation and remaining measurements

Current lightweight regression: **22 passed**, three PostgreSQL cases excluded.
Checks cover exact shifted windows, ordinary neighboring windows, complete visible
number/negation/attribution changes, `(five)`, `(пять)`, `(only)`, both original
contexts, oversized and multiple diffs staying separate, 100-alias pagination,
bounded authorization scopes and tool-field instruction preservation. Ruff and
`git diff --check` pass. The actual saved longitude manifest validates unchanged.

Before the final variant alignment change, a combined PostgreSQL regression suite
passed 47 tests. An intermediate strict-only full replay reused all document
vectors (zero passage embeddings) and verified old pinned pagination unchanged:
51 documents; preparation 3.44 seconds / 2150 SQL calls; 25 queries 259.96 seconds /
8740 SQL calls, 25 query embeddings and 3965 reranked passages. Process peak RSS
was 1.19 GB (cumulative, including both policies). That strict-only version freed
no problematic top-six slots, so its results do not qualify the final version.

The baseline has saved rankings, but no comparable original timing, SQL/network
or memory measurements. Final full SQL/ONNX replay, current PostgreSQL regression,
bytes read/written, incremental memory and corpus/history growth measurements
remain unverified: the shared heavy-test slot belongs to the coordinator. The
grouping-only result must not be reported as complete end-to-end qualification.
