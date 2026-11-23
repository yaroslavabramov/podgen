# Editorial recheck after a rewrite

In new `production/1` runs the first `reader-review` reads the speech independently. Subsequent `reader-review-1/2` stages use `editorial-recheck/2`. The rechecker receives the full current script and `revision_check`: the previous script reconstructed losslessly from changed turns and references to unchanged ones, the exact mandatory tasks delivered to the writer, the revision operation and the previous reader's recommendation, strengths, limits and scope. These inputs come from saved requests and reviews.

The rechecker decides whether each assigned problem was solved as proposed, solved differently, removed together with its passage or remains open. Evaluate late findings against the entire new script, including downstream consequences. A factual task's effect on the story is visible to the script reader. The independent verifier checks its evidentiary support.

Under version 2, `still_open` on a mandatory task must reference an `important` or `blocking` finding and cannot coexist with `keep`. Validate the same invariant at receipt, replay, resume and export. The manifest pins the contract, instruction and response schema. Version 1 retains its original response and revision-guidance shape.

The writing outcome requires two recommendations on the same script. Request validation and structural consistency do not by themselves establish listening quality. See [the reading audit](reading-audit-contract.md) and [technical recovery](technical-recovery.md).
