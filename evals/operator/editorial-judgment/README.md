# Editorial decisions in a plan

Seven paired choices exercise a planner's editor on a saved revision of the November 7 Snow plan. The dossier and plan hashes are pinned in [pairs.json](pairs.json). Each control changes a specific passage and asks which editorial action it warrants. The operator judges that action independently of the verdict on the whole plan.

| Case | Decision under review |
| --- | --- |
| `rich-facts` | Preserve distinct workhouse, brewery and water delivery evidence when each advances the story. A quota of one clue per act loses material. |
| `scene-detail` | Allow an incidental sound in a reconstructed bottle delivery when it supplies atmosphere. |
| `repeated-justification` | Retain the causal qualification at the decline of the outbreak and remove repeated explanations elsewhere. |
| `unused-number` | The bottle links the pump to consumption. Its volume would matter to a dose calculation, which this version never makes. |
| `needed-denominator` | A risk comparison using five workhouse deaths needs the already documented 535 residents. |
| `chronology` | Correct a scene that presents an April 1855 inspection as grounds for the September 1854 decision. |
| `invented-evidence` | The revised bottle scene invents a sample test and its decisive presentation to the board. Remove those events while retaining legitimate details. |

## Generate offline requests

From the root of a checkout with the backend dependencies installed, run the [operator pair tests](../../../backend/tests/test_editorial_operator_pairs.py). Choose a new temporary directory for each run because pytest clears its supplied base directory.

```sh
uv run --offline --directory backend pytest -q tests/test_editorial_operator_pairs.py \
  --basetemp="$(mktemp -d)"
```

Each case produces a `request.json` built by the archived `existing/1` plan-review preview, a `candidate.json` suitable for the stage's normal candidate input, an `operator.json` with the alternatives and expected decision, and a `provenance.json` with source hashes, mapping from local cards to persistent IDs and the projection version. The assertions check construction and data separation. They make no provider calls.

Show the reviewer the request alone. Keep its full answer, actual profile, request, attempts and local rationale. Then reveal the operator key and compare the reasoning and effect of the proposed edit. Record other reasonable actions and unrelated problems separately. The schemas can verify targets, references and speaker membership. The value of a scene detail or number is an editorial decision.

For a pair of full-plan reviews with an independently stored answer key, see the [P01/P02 procedure](review-control.md).
