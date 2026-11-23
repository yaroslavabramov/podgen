# Paired plan review

P01 and P02 are complete solo Snow plans on the same brief and dossier. The ordinary reviewer receives the same profile, evidence, response schema and stage instructions. The operator changes three passages.

| Passage | P01 | P02 |
| --- | --- | --- |
| Opening | A permissible scene followed by a general prohibition on scenes and sounds. | The same scene with incidental wheel noise and the prohibition removed. |
| Brewery | A rebuttal to a claim that beer was curative. The plan never made that claim. | The same observations about the brewery and its water supply. |
| Pump handle | The April 1855 inspection is presented as grounds for the September 1854 action. | The dates and the decline before the handle's removal are in the right order. |

Either plan may merit revision elsewhere. P02 controls only these three local choices. The operator introduced both defects and corrections. One exploratory sentence, C03, reproduces an archived model response. The [control definitions](review-controls.json) pin the source dossier and plan by SHA-256.

## Reproduce the saved experiment

Use a separate checkout of `52c07127` with its backend dependencies. This revision reproduced all four outputs offline. The current operator assumes an older request layout and stops when `production/1` omits the top-level `dossier` it expects. The [local paired controls](README.md) remain available in this checkout.

Use a complete `ModelProfile` JSON for role `plan_reviewer`. The profile-create command prints the complete configuration under `configuration`. Its `--out` file holds the reference. For an existing run, the saved manifest identifies the reviewer's profile reference.

```sh
uv run --offline --directory backend python \
  ../evals/operator/editorial-judgment/prepare_review_control.py \
  --profile /path/to/ModelProfile.json --out /path/to/new-review-control
```

The [preparer](prepare_review_control.py) requires a new directory. It verifies source hashes and quote anchors, compiles candidates with the normal preview, and checks that both versions have matching context and schema. It writes four files.

| File | Use |
| --- | --- |
| `requests.jsonl` | Two full provider request bodies, one per opaque custom ID. Only the body goes to the provider. |
| `manual-key.json` | Operator-only JSON pointers, quotation anchors, expected diagnosis and action. |
| `offline-requests.jsonl` | Seven exploratory C01–C07 requests, separate from the main pair. |
| `manifest.json` | Source and request hashes, shared profile, prompt and projection versions, and `model_called=false`. |

## Score

If a live comparison is authorized, retain the exact request, complete provider response and structured review, usage and attempt identifier. For each target, record the quoted finding or strength, its target, what the reviewer observed, its proposed action and the meaning to retain. Score each as a hit, miss, false alarm or unresolved and explain the decision.

For P01 a hit requires an accurate diagnosis and a remedy that addresses it. The brewery passage needs the invented rebuttal removed. The later investigation can stay when correctly dated. For P02 record a false alarm if a mandatory edit bans the harmless sound, reintroduces the beer rebuttal or removes a consequential distinction between dates. Assess independent findings, stylistic suggestions and research requests separately. C07 expressly forbids invented sound or domestic detail in the client brief. Score that case under its own brief.
