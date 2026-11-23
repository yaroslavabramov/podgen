# Story planning and writing

The planner selects a way through the material, the writer delivers spoken text, and an independent reader assesses what a listener could understand from that text alone. A source can be useful to the writer without appearing on air. A line belongs in the script when it advances the explanation, establishes a necessary fact, or gives the listener a moment worth hearing.

## Plan a complete account

The plan names the central question, listener promise, working answer, opening, development and ending. Each act changes what the audience understands. Specify what must already be clear and how the new scene, observation or decision makes the next act possible. Keep facts, source provenance and creative choices distinguishable. Available evidence leaves the planner free to choose its strongest route.

```text
StoryPlan.payload = {
  episode_spec_ref, dossier_ref, editorial_decision_ref,
  main_question, listener_promise, working_answer,
  selected_option_id, considered_options: StoryOption[],
  opening: {approach, material_refs[], question_to_open},
  acts: ActPlan[],
  ending: {answer_to_deliver, promises_to_close[]},
  host_functions: [{speaker_id, contribution, interaction_guidance, speech_register}],
  duration_strategy: {estimated_seconds_range, assumptions, optional_material[]},
  unresolved_research_request_refs: ArtifactRef[]
}
StoryOption = {
  option_id, angle, route_summary, strongest_material_refs[],
  listener_benefit, weakness, research_needed[], selection_reason|null
}
ActPlan = {
  plan_act_id, title, listener_question, intended_change_in_understanding,
  prerequisite_concepts[], material_refs[],
  transitions: {from_previous, toward_next},
  promises_opened[], promises_addressed[],
  estimated_seconds_range, priority: essential|optional
}
```

The implemented own-plan workflow generates a candidate and sends that precise candidate to the plan editor. A revised candidate receives a second reading. The writer can only use an explicitly cleared plan. Fixed-plan evaluation supplies a preselected plan to isolate writing performance. See [planning behavior](planning-v9.md) and [role inputs](08-editorial-inputs.md).

## Script identity and edits

Spoken words, production notes, evidence metadata and instructions are separate fields. The script orders the utterances and identifies the speaker. A stage direction may guide performance, but cannot supply a causal link that the listener would otherwise miss.

```text
ScriptRevision.payload = {
  episode_spec_ref,
  story_plan_ref: ArtifactRef|null, dossier_ref: ArtifactRef|null,
  parent_script_ref: ArtifactRef|null,
  mode: exploratory|production,
  completion: fragment|draft|complete,
  acts: ScriptAct[],
  author_support_bindings: SupportBinding[],
  plan_departures: [{description, rationale, affected_plan_act_ids[]}],
  open_research_request_refs: ArtifactRef[]
}
ScriptAct = {act_id, plan_act_id|null, title, draft_tail: open|closed, blocks: ScriptBlock[]}
ScriptBlock = {block_id, function_note|null, items: ScriptItem[]}
ScriptItem =
  {kind: utterance, utterance_id, speaker_id, text}
  | {kind: stage_direction, direction_id, purpose, text}
  | {kind: research_placeholder, placeholder_id, question, request_ref|null}
LocalScriptSelector = {
  act_id, block_id, item_id,
  char_start: integer|null, char_end: integer|null
}
ScriptSelector = {script_ref: ArtifactRef, selector: LocalScriptSelector}
SupportBinding = {
  selector: LocalScriptSelector, basis_refs: ArtifactRef[],
  relation: direct|interpretation|illustration, note: string|null
}
```

An edit binds to the exact base revision. Retained passages keep their identity where possible. A split, merge or move has an explicit mapping. Save the new revision before deriving its change set to avoid a cycle between their references. Recompute affected continuity and review judgments. A favorable review of a previous version is provenance.

```text
ScriptChangeSet.payload = {
  base_script_ref, next_script_ref, change_reason,
  operations: [{kind, before_selectors[], after_selectors[], rationale}],
  identity_map: [{old_item_ids[], new_item_ids[], relation: retained|split|merged|removed|inserted}],
  research_request_dispositions: [{request_ref, action: still_open|addressed|obsolete,
                                 new_selectors[], rationale}],
  dependency_dispositions: AuthorDependencyProposal[],
  declared_impact: {structure, spoken_text, evidence_basis, speaker_assignment}
}
```

## Writing at length

A long-form strategy is an evaluation question. Compare whole drafts, act-based drafting and act-based revisions on the same assignment and evidence. A successful outline or an accepted short script alone cannot validate a 60-minute narrative. The current production evaluation uses a whole draft followed by bounded revisions.

For act-based writing, pass the writer the actual preceding speech, what listeners already know, unresolved questions, established terminology and the named purpose of the next act. Store completed blocks separately if a model cannot return a whole act within one response. A retried append must refer to its original operation and base version. Never silently truncate the ending.

```text
ListenerState.payload = {
  script_ref,
  covered_prefix: {through_item_id: UUID|null, ordered_item_ids[], content_and_order_hash},
  audience_assumptions: [{assumption, basis: episode_spec}],
  introduced_entities: [{entity, aliases[], introduction_selectors[]}],
  explained_concepts: [{concept, explanation_summary, supporting_selectors[], depth}],
  established_connections: [{connection, supporting_selectors[], qualification}],
  questions_and_promises: [{statement, opened_at[], status: open|partly_answered|answered|explicitly_unknown,
                          addressed_at[], rationale}],
  comprehension_risks: [{description, selectors[], proposed_action}],
  extractor_profile_ref
}
```

Continuity comes from the current words. Recompute listener state after a move or deletion. A plan might say that a character has learned something before a meeting while the draft only reveals it afterward. Reread both the transition and its later consequence against the revised order.

## Revision, duration and performance

The reader locates a concrete problem in the spoken account. The fact-checker independently supplies the supported meaning. The author may resolve several findings with one coherent edit. Removing an unnecessary claim may also remove its factual problem. A factual note does not oblige the author to preserve an obsolete paragraph.

The editor rereads the complete result. Check the first point of confusion, repeated conclusions, dialogue that merely restates the preceding speaker, and a finale that promises more than the episode delivers. Use the configured voice’s duration estimate to shape the amount of substantive material. The actual master supplies the measured time. Route every semantic correction through a new script revision before it reaches the audio stage.

The [editorial practice guide](03-editorial-practice.md) illustrates selection, spoken explanation and factual repair. [Verification](04-verification-and-evaluation.md) defines independent reading and release criteria.
