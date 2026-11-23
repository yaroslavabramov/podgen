# Research and editorial direction

An episode needs an explanation rich enough to support a story. A list of extracted facts rarely explains what a participant knew, why a decision was made, or which later discoveries changed the interpretation. The research owner develops connected notes; the editor decides what the episode needs from them.

The current production path commissions [hosted research](research/2026-11-13-hosted-research-integration.md), retains the source corpus and gives the writing roles tools to inspect it. The contracts below describe the more general research and editorial design.

## Questions and decisions

An editorial state connects the user's assignment to current questions, accepted notes and the decision to proceed. Authors can ask for an investigation when a gap affects an actual passage. The editor weighs the significance of the gap against the alternatives of narrowing or dropping the passage.

**Editorial state**

```text
episode_spec_ref: ArtifactRef
dossier_ref: ArtifactRef | null
main_question: string
working_answer: string | null
answer_basis_refs: ArtifactRef[]
alternatives: AlternativeExplanation[]
listener_promise: string | null
active_question_refs: ArtifactRef[]
story_plan_ref: ArtifactRef | null
script_ref: ArtifactRef | null
current_decision_refs: ArtifactRef[]
phase: orienting | researching | planning | drafting | editing | verifying | producing_audio | ready
```

**Question record**

```text
question: string
why_it_matters: string
scope: {subject, geography, temporal: EpisodeSpec.temporal}
origin_ref: ArtifactRef                         # EpisodeSpec, ResearchRequest, EditorialDecision
acceptance_questions: string[]
known_note_refs: ArtifactRef[]
competing_explanations: string[]
impact: central_answer | local_passage | enrichment
resolution: open | answered | partial | deferred | unanswerable
answer_note_refs: ArtifactRef[]
uncertainty_refs: ArtifactRef[]
resolution_reason: string | null
```

A research note contains a connected answer, exact evidence, limitations and competing explanations. Attach evidence to the passage it supports, using Unicode code-point offsets in the complete note. Preview generation happens after persistence. It has its own source reference and may never overwrite the full explanation.

**Research note**

```text
question_refs: ArtifactRef[]
title: string
answer_markdown: string                         # complete connected answer
citations: NoteCitation[]
interpretations: InterpretationBinding[]
limitations: string[]
uncertainty_refs: ArtifactRef[]
supersedes_note_refs: ArtifactRef[]
```

Classify uncertainty by cause: an inaccessible item, a gap in the collected corpus, incomplete investigation, disagreement between sources or genuine uncertainty about the event. A failed search cannot prove that the world lacks an answer. Record which part of the episode is affected, the available evidence, how the question can be resolved and the editorial treatment chosen.

**Uncertainty record**

```text
question_ref: ArtifactRef
description: string
kind: acquisition_blocked | corpus_gap | research_incomplete | conflicting_evidence | world_unknown
basis_refs: ArtifactRef[]
search_attempt_ids: UUID[]                     # IDs in the append-only search record
affected_statement: string
impact: central_answer | local_passage | enrichment
handling: investigate | qualify | omit | explicit_unknown | defer
next_action: string | null
resolution_note_ref: ArtifactRef | null
```

**Dossier snapshot**

```text
episode_spec_ref: ArtifactRef
knowledge_as_of: Timestamp                      # exact EpisodeSpec.temporal.knowledge_as_of
source_policy_ref: ArtifactRef                  # kind Policy, policy_kind=source
overview_note_ref: ArtifactRef
timeline_note_ref: ArtifactRef | null
note_refs: ArtifactRef[]
question_refs: ArtifactRef[]
uncertainty_refs: ArtifactRef[]
source_revision_refs: ArtifactRef[]
evidence_refs: ArtifactRef[]
candidate_materials: [{note_ref, text_selector|null, use: mechanism|scene|example|counterexample|quote}]
excluded_topics: [{topic, reason, decision_ref}]
```

The dossier freezes the state admitted to the planner. A review can distinguish a fact available before a historical decision from later evidence for that decision. An editorial decision records which material was integrated, the resulting state and why it changes the episode. A targeted research request names the missing information, the reason it matters, the affected wording and previously tried sources.

**Editorial decision**

```text
state_ref: ArtifactRef
action: commission | integrate | change_direction | proceed | revise | narrow_scope | stop
reason: string
considered_options: [{option, benefit, unresolved_problem, basis_refs[]}]
selected_option: string
input_result_ids: UUID[]                       # runtime task_results.result_id
question_refs: ArtifactRef[]
reviewed_output_refs: ArtifactRef[]            # exact existing outputs reviewed
dependency_dispositions: [{subject_ref: ArtifactRef, dispositions: DependencyDisposition[]}]
request_dispositions: [{request_ref: ArtifactRef, action: still_open|addressed|obsolete,
                       current_subject_ref: ArtifactRef, current_location: object|null,
                       basis_refs: ArtifactRef[], reason: string}]
expected_effect: string
remaining_risks: string[]
```

**Targeted research request**

```text
requester_ref: ArtifactRef                     # ScriptRevision, StoryPlan, VerificationReport, EditorialDecision
location_selector: object | null              # typed selector for the requester
question: string
why_it_matters: string
current_understanding: string
context_refs: ArtifactRef[]
acceptance_questions: string[]
impact: central_answer | local_passage | enrichment
blocking: none | passage | central_answer
requested_outcome: explanation | support | contradiction | example
```

## Research loop

1. Start from the user's actual topic, audience, duration and format. Do not fill the brief with an editor's preferred answer.
2. Orient the investigation. Identify the central unknown, useful primary accounts, independent interpretations, and the practical constraints on obtaining them.
3. Investigate related questions together when they share documents or depend on each other. Independent workers can return results to the same owner. Their completed tasks do not automatically become accepted dossier entries.
4. Check names, roles, chronology, mechanisms, important numbers and quotations in the source. A late account can document what the author later reported without establishing that all its details were known at an earlier meeting.
5. Stop once the material supports the listener’s promised answer and the remaining uncertainty can be handled honestly through selection or wording. Save significant alternatives, including the choice to omit them.

A draft can expose a missing link in the explanation. The writer asks about that link in context, the research subsystem returns the result with sources, and the editor reopens the affected part of the story. Bind the request and result to the same question and script revision across recovery. The [runtime handoff](research-corpus-runtime.md) describes the implemented flow.

## Concurrent integration

Researchers return results to an editorial inbox. The owner checks the expected head, current question and existing evidence before adopting a result. If another decision advances the head while research is in flight, re-evaluate the result against that newer state. Late material can remain valuable without replacing the accepted dossier.

```text
read(head=H, results=R)
compile complete context pinned to H and R
model -> proposal: decision, note changes, result dispositions
validate references, scope, selectors, schemas
transaction:
  assert current generation/fencing token and current head == H
  persist changed notes/questions/uncertainties
  persist DossierSnapshot D, EditorialDecision J, EditorialState H2
  record integration outcomes for R
  compare-and-swap head H -> H2
  append outbox events
```

## Evaluation

Compare the proposed flow with one strong model using simple tools. Both receive the same topic, dates, language, duration, access to sources and independent final assessment. To isolate writing quality, also compare authors on the same frozen expert dossier. Score the resulting explanations and episodes. Record omissions, unsupported central assertions and recovery after genuine gaps. Search volume is diagnostic.
