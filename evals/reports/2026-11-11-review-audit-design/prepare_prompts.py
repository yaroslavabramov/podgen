"""Build reviewable immutable drafts; do not move production pointers."""

import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
ASSETS = ROOT / "backend/src/podgen/writing/behavior/assets"
OLD = {
    "reader": "51752e323aff6ff6b285a0445b5a828a670b19baab7352ce44e2a4164f4ebcda",
    "reader_recheck": "38df9c99e8642fde657263cdae2c87744f3dd0dca36efad10fb679879f110239",
    "fact": "62b7066f83b9aa7b486fc1e5825b56c01ae1725d5d2ad449252e3c2b18cdd4f2",
}
sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
base = {k: (ASSETS / (v + ".md")).read_text() for k, v in OLD.items()}
for k, v in base.items():
    assert sha(v) == OLD[k]

READER_AUDIT = """## Record the complete editorial audit

In `listener_answer`, describe the understanding listeners develop as the speech unfolds. In `audit.judgments`, record the contextual meaning, scope, listening effect and rationale of decisions about acceptable passages as well as passages needing change. Cover story development, clarity, transitions, repetition, ending, voice and idiom. The same passage may bear on several aspects; the areas do not prescribe an episode structure or require a defect under each heading.

In `audit.coverage`, account for all current utterances through inclusive ranges and link the decisions recording the work. In `audit.areas`, account for each editorial aspect. Mark work `unchecked` when it was not assessed sufficiently, and explain what remains; use `not_applicable` only with a concrete reason. A range marked reviewed does not by itself establish that its judgments are correct. Empty findings do not establish complete coverage. In `audit.overall_judgment`, explain the recording decision by connecting the passage judgments to how the complete telling develops and how its voice, rhythm and perspective work for this audience.

Give each decision a unique `decision_id`. Author `memory_summary` separately, within its 1600-character bound, while keeping complete contextual meanings and rationales in the full audit. Link every finding to the decisions establishing it in `finding_links`. Keep audit judgments and memory separate from spoken material and from the writer's repair assignments.

"""
FACT_AUDIT = """## Record the complete factual audit

In `audit.claim_checks`, record supported claims as well as contradicted, unsupported and unchecked claims, at the contextual meaning established from the speech. A check may assess several related claim types. The first audit covers the full contextual account, not only suspicious sentences; a recheck maintains that coverage by carrying forward applicable decisions and checking what requires fresh assessment.

Record the inspected evidence and rationale for each verdict. `supported` requires a completed comparison; `contradicted` requires incompatible evidence; `unsupported` records an assertion still unestablished after the relevant retained-source investigation; `unchecked` identifies work not sufficiently performed. An inability to finish a check is not evidence that its claim is false. Account for all utterances in `audit.coverage`, including explicit unchecked ranges and justified non-claim material, and account separately for factual accuracy, causality, chronology and generalization in `audit.areas`. Checking dates alone does not check the causal account built from them. Coverage records work performed, not a guarantee that every inference was extracted correctly.

Use `source_refs` for the actual evidence behind each decision. Offered B aliases refer to evidence delivered in the current packet, successful E reads to passages inspected in this review, and H aliases to frozen evidence retained from the previous own-role audit. Set `from_review` accordingly and preserve the offered alias exactly; a previous E identifier must not be mistaken for a new read with the same local name. The evidence and its recorded scope, not a prior verdict alone, support an inherited factual decision.

Give each decision a unique `decision_id` and a separately authored `memory_summary` of at most 1600 characters. Keep the full meaning, rationale and source bindings in the complete audit. In `audit.overall_judgment`, assess the factual account as a whole. Link every finding to the decisions establishing it in `finding_links`. Unchecked consequential work remains explicit; assess its material effect and whether an evidence gap prevents an adequate bounded account rather than automatically assigning another narrated caution.

"""
INITIAL = """## Initial decisions and revision work

This is the first audit of the supplied episode. Collect the demonstrated required work before commissioning its revision. Return empty `assignment_results`, `previous_dispositions` and `late_findings`, and null for `effect_on_episode`: there is no revision to compare. A complete audit includes justified positive decisions and explicit unchecked work; it does not promise infallibility or require a finding for every topic.

"""
RECHECK = """## Verify the revision against the prior audit

Use `audit_context`: its `previous_decisions`, `previous_coverage`, `previous_recommendation` and `previous_overall_judgment` record your previous own-role audit; its `assignments` record the work actually transmitted to the writer. Compare that context with the current script and actual changes. The previous recommendation is context to verify, not a verdict to repeat. Keep the other role's diagnosis distinct from your own assessment. The writer's report of completion is not a substitute for comparing the speech.

Reconstruct the previous script from `audit_context.previous_script_in_order` in block order: copy current utterances from each `copy_current.first` through `last`, inclusive, and insert the exact `previous_turns` at their positions. Number the reconstructed previous turns from zero in the restored order, ignoring current-script index values carried by copied turns. Previous assignment and audit anchors refer to that reconstructed order; your current audit, findings and permissions address the complete current script.

For every previous decision return one `previous_dispositions` entry. Use `retained` when its contextual meaning and basis remain applicable, `reassessed` when they require a fresh decision, and `retired` when the current account has removed the matter being assessed. State the reason and link the current decisions carrying its result. Unchanged wording alone does not establish unchanged context, and a change in wording alone does not invalidate an equivalent supported meaning. Maintain a complete current audit by carrying forward applicable decisions and examining changed claims, joins, dependent passages and effects on the whole episode. Do not silently reinterpret unchanged acceptable speech under a wider scope.

Account for every supplied writer assignment exactly once in `assignment_results`. Judge the underlying problem and the useful supported meaning in `preserve`, rather than literal compliance with the proposed `action`. Use `achieved`, `solved_differently`, `no_longer_needed` or `still_open` according to what the current speech accomplishes. For `still_open`, commission a required current finding and set `followup_finding` to its zero-based index; otherwise use null. Read `audit_context.revision_operation` as the permissions the writer actually received, not as permission to alter surrounding speech in your next assignment.

For each later mandatory finding, return a linked `late_findings` entry. Establish the actual unresolved assignment, error introduced by revision, change in context or evidence, or substantial issue missed in the previous audit. Link the actual previous decision IDs or zero-based `previous_coverage_indices` into `audit_context.previous_coverage` locating the prior gap, with current decision IDs and assignment IDs where relevant. For genuinely new material, link its current decisions without inventing a prior decision. Explain the concrete grounds and substantive consequence; repeating `reason_kind` does not establish necessity. A genuine substantial omission may be corrected even in unchanged speech, but a newly imaginable literal interpretation or preference between equally adequate tellings does not justify another rewrite. Optional polish remains optional. An earlier approval neither exempts a real error nor supplies a reason to invent one.

In `effect_on_episode`, assess what the edit gained, preserved or lost; it is the revision comparison, not another summary of the current episode. Keep your rationale, audit history and evidence checks private to review; only current required repairs become writer assignments.

"""
READER_FACT_BOUNDARY = """Assess R and F assignments for execution and listening effect. The reader receives no original fact-source evidence or fact audit history. A factual task's diagnosis is not independent evidence. Do not certify its truth from the task alone or leave a visibly completed task open merely because sources are absent here; fact-review separately verifies support. You may identify contradictions or missing connections created by the present speech.

"""
FACT_RECHECK_BOUNDARY = """First assess whether each previous factual decision still applies, using the full frozen evidence in `audit_context.previous_evidence` and its recorded scope. Neither an assignment nor the previous verdict substitutes for that evidence. Carry forward the decision when its contextual meaning, surrounding account and evidence basis remain valid. Inspect the source again when those materials do not settle applicability or the current claim. Investigate new, changed, context-invalidated or previously unverified claims; a required check that was never completed remains unchecked, not silently approved. A new review round alone does not require repeating valid source investigation.

"""
reader_body = base["reader"][base["reader"].index("## Judge the complete telling") :]
reader_body = reader_body.replace(
    "Put these judgments in `listener_answer`, supported by concrete words from the relevant passages.",
    "Ground these judgments in concrete words from the relevant passages.",
)
reader_judgment, reader_commission = reader_body.split(
    "## Commission the needed work", 1
)
reader_first = (
    base["reader"].split("## Judge the complete telling")[0]
    + reader_judgment
    + READER_AUDIT
    + INITIAL
    + "## Commission the needed work"
    + reader_commission
)
reader_intro = """# Read the revised episode as a finished telling

Decide whether you would send the complete current script to the assigned speakers to record. Read its spoken words in order for its genre, format and audience, then examine the supplied revision context. Report `reading_mode=informed`. Use `output_language` for prose and preserve speaker labels, quotations and keys exactly.

"""
reader_repeat = (
    reader_intro
    + RECHECK.replace(
        "Account for every supplied writer assignment exactly once",
        "Account for every supplied R and F writer assignment exactly once",
    )
    + READER_FACT_BOUNDARY
    + reader_judgment
    + READER_AUDIT
    + "## Commission the needed work"
    + reader_commission
)
fact_body = base["fact"].replace(
    "In `listener_answer`, reconstruct the material assertions at that contextual scope.",
    "In `listener_answer`, describe the factual account this episode conveys. Record individual contextual claims and their decisions in `audit.claim_checks`.",
)
fact_body = fact_body.replace(
    "A possible isolated literal reading is a reason to inspect the passage, not yet a factual finding.",
    "A possible isolated literal reading is a reason to inspect the passage, not yet a factual finding. Do not broaden a claim to all places or times unless the connected speech actually conveys that scope.",
)
fact_body = fact_body.replace(
    "In `source_read_ids`, copy the returned E-identifiers of the read passages on which this finding depends; use `[]` when the original cards suffice.",
    "In `source_read_ids`, copy the exact current E-read aliases or retained H-evidence aliases on which this finding and its correction depend; use `[]` when the original cards suffice.",
)
head, tail = fact_body.split("## Establish the claim before checking it", 1)
fact_judgment, fact_repair = tail.split("## Give the writer an adequate repair", 1)
fact_first = (
    head
    + "## Establish the claim before checking it"
    + fact_judgment
    + FACT_AUDIT
    + INITIAL
    + "## Give the writer an adequate repair"
    + fact_repair
)
fact_repeat_judgment = fact_judgment.replace(
    "Independently verify precise details:",
    "For claims requiring fresh assessment, independently verify precise details:",
)
fact_repeat_judgment = fact_repeat_judgment.replace(
    "When the supplied excerpts do not settle a detail, investigate it in the retained sources before treating it as unsupported.",
    "When the supplied excerpts and applicable retained evidence do not settle a detail, investigate it in the retained sources before treating it as unsupported.",
)
old_investigation = "Use `search_sources` to locate relevant passages in the retained sources and `read_source` to inspect them with enough surrounding text to establish their meaning. Before recommending removal or weakening for lack of support, or requesting new material, perform this check: the supplied excerpts are an entry point, not the boundary of the evidence. Base an absence-of-support finding on the actual search and reading result. If a source cannot be read or the detail remains unverified, report that coverage gap accurately."
new_investigation = "For claims needing investigation, use `search_sources` to locate relevant passages in the retained sources and `read_source` to inspect them with enough surrounding text to establish their meaning. Before recommending removal or weakening for lack of support, or requesting new material, establish the actual relevant search and reading result: an applicable completed investigation preserved in the audit may satisfy this requirement; otherwise perform it now. Supplied excerpts are an entry point, not the boundary of the evidence. Do not search or read the same valid frozen evidence again solely because another round has begun. Inspect again when the current claim, context, evidential scope or unresolved question requires it. If a source cannot be read or the detail remains unverified, report that coverage gap accurately."
assert old_investigation in fact_repeat_judgment
fact_repeat_judgment = fact_repeat_judgment.replace(
    old_investigation, new_investigation
)
fact_repeat = (
    head
    + RECHECK.replace(
        "Account for every supplied writer assignment exactly once",
        "Account for every supplied F writer assignment exactly once",
    )
    + FACT_RECHECK_BOUNDARY
    + "## Establish the claim before checking it"
    + fact_repeat_judgment
    + FACT_AUDIT
    + "## Give the writer an adequate repair"
    + fact_repair
)
rows = []
for role, body, baseline in [
    ("reader-first", reader_first, "reader"),
    ("reader-recheck", reader_repeat, "reader_recheck"),
    ("fact-first", fact_first, "fact"),
    ("fact-recheck", fact_repeat, "fact"),
]:
    identity = sha(body)
    (ASSETS / (identity + ".md")).write_text(body)
    for name, text in [("before", base[baseline]), ("after", body)]:
        (OUT / f"{role}-{name}.md").write_text(text)
    diff = difflib.unified_diff(
        base[baseline].splitlines(),
        body.splitlines(),
        fromfile="production/" + role,
        tofile="candidate/" + role,
        n=10000,
        lineterm="",
    )
    (OUT / f"{role}-full-comparison.md").write_text(
        "# Complete instruction comparison\n\n```diff\n"
        + "\n".join(x.rstrip() for x in diff)
        + "\n```\n"
    )
    rows.append(
        {
            "role": role,
            "before_sha256": OLD[baseline],
            "sha256": identity,
            "words": len(body.split()),
            "asset": f"backend/src/podgen/writing/behavior/assets/{identity}.md",
        }
    )
for paragraph in (
    base["reader"].split("## Judge the complete telling", 1)[1].split("\n\n")[2:]
):
    if paragraph.strip():
        assert paragraph in reader_first and paragraph in reader_repeat
(OUT / "proposed-assets.json").write_text(
    json.dumps(
        {
            "status": "reviewed instruction assets; activation is tracked in production.json and implementation.md",
            "roles": rows,
            "api_calls": 0,
        },
        indent=2,
    )
    + "\n"
)
print(json.dumps(rows, indent=2))
