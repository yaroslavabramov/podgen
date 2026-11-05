from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import UUID, ArtifactRef, Contract, LocalScriptSelector, NonNegativeInt, ScriptSelector, Text


class SecondsRange(Contract):
    min_seconds: NonNegativeInt
    max_seconds: NonNegativeInt

    @model_validator(mode="after")
    def ordered(self):
        if self.max_seconds < self.min_seconds:
            raise ValueError("invalid_seconds_range")
        return self


class StoryOption(Contract):
    option_id: UUID
    angle: Text
    route_summary: Text
    strongest_material_refs: list[ArtifactRef]
    listener_benefit: Text
    weakness: str
    research_needed: list[str]
    selection_reason: str | None


class Transitions(Contract):
    from_previous: str
    toward_next: str


class ActPlan(Contract):
    plan_act_id: UUID
    title: Text
    listener_question: Text
    intended_change_in_understanding: Text
    prerequisite_concepts: list[str]
    material_refs: list[ArtifactRef]
    limitations_to_voice: list[str]
    transitions: Transitions
    promises_opened: list[str]
    promises_addressed: list[str]
    estimated_seconds_range: SecondsRange
    priority: Literal["essential", "optional"]


class Opening(Contract):
    approach: Text
    material_refs: list[ArtifactRef]
    question_to_open: Text


class Ending(Contract):
    answer_to_deliver: Text
    qualifications: list[str]
    promises_to_close: list[str]


class HostFunction(Contract):
    speaker_id: UUID
    contribution: Text
    interaction_guidance: Text
    speech_register: Text


class DurationStrategy(Contract):
    estimated_seconds_range: SecondsRange
    assumptions: list[str]
    optional_material: list[str]


class StoryPlan(Contract):
    episode_spec_ref: ArtifactRef
    dossier_ref: ArtifactRef
    editorial_decision_ref: ArtifactRef
    main_question: Text
    listener_promise: Text
    working_answer: Text
    selected_option_id: UUID
    considered_options: list[StoryOption]
    opening: Opening
    acts: list[ActPlan]
    ending: Ending
    host_functions: list[HostFunction]
    duration_strategy: DurationStrategy
    unresolved_research_request_refs: list[ArtifactRef]

    @model_validator(mode="after")
    def selection(self):
        if self.selected_option_id not in {o.option_id for o in self.considered_options}:
            raise ValueError("selected_story_option_missing")
        return self


class Utterance(Contract):
    kind: Literal["utterance"]
    utterance_id: UUID
    speaker_id: UUID
    text: Text


class StageDirection(Contract):
    kind: Literal["stage_direction"]
    direction_id: UUID
    purpose: Text
    text: Text


class ResearchPlaceholder(Contract):
    kind: Literal["research_placeholder"]
    placeholder_id: UUID
    question: Text
    request_ref: ArtifactRef | None


ScriptItem = Annotated[Utterance | StageDirection | ResearchPlaceholder, Field(discriminator="kind")]


class ScriptBlock(Contract):
    block_id: UUID
    function_note: str | None
    items: list[ScriptItem]


class ScriptAct(Contract):
    act_id: UUID
    plan_act_id: UUID | None
    title: Text
    draft_tail: Literal["open", "closed"]
    blocks: list[ScriptBlock]


class SupportBinding(Contract):
    selector: LocalScriptSelector
    basis_refs: list[ArtifactRef]
    relation: Literal["direct", "interpretation", "illustration"]
    note: str | None


class PlanDeparture(Contract):
    description: Text
    rationale: Text
    affected_plan_act_ids: list[UUID]


class ScriptRevision(Contract):
    episode_spec_ref: ArtifactRef
    story_plan_ref: ArtifactRef | None
    dossier_ref: ArtifactRef | None
    parent_script_ref: ArtifactRef | None
    mode: Literal["exploratory", "production"]
    completion: Literal["fragment", "draft", "complete"]
    acts: list[ScriptAct]
    author_support_bindings: list[SupportBinding]
    plan_departures: list[PlanDeparture]
    open_research_request_refs: list[ArtifactRef]

    @model_validator(mode="after")
    def structure(self):
        if self.mode == "production" and (self.story_plan_ref is None or self.dossier_ref is None):
            raise ValueError("production_requires_plan_and_dossier")
        ids = []
        utterances = 0
        for act in self.acts:
            ids.append(act.act_id)
            if self.completion == "complete" and act.draft_tail != "closed":
                raise ValueError("complete_script_has_open_tail")
            for block in act.blocks:
                ids.append(block.block_id)
                for item in block.items:
                    ids.append(item_id(item))
                    utterances += isinstance(item, Utterance)
                    if self.completion == "complete" and isinstance(item, ResearchPlaceholder):
                        raise ValueError("complete_script_has_placeholder")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_script_id")
        if self.completion == "complete" and not utterances:
            raise ValueError("complete_script_requires_spoken_text")
        return self


def item_id(item: ScriptItem) -> UUID:
    return getattr(
        item,
        {
            "utterance": "utterance_id",
            "stage_direction": "direction_id",
            "research_placeholder": "placeholder_id",
        }[item.kind],
    )


class ChangeOperation(Contract):
    kind: Text
    before_selectors: list[ScriptSelector]
    after_selectors: list[ScriptSelector]
    rationale: Text


class IdentityMapping(Contract):
    old_item_ids: list[UUID]
    new_item_ids: list[UUID]
    relation: Literal["retained", "split", "merged", "removed", "inserted"]


class AuthorDependencyProposal(Contract):
    input_ref: ArtifactRef
    proposed_action: Literal["retained", "replaced", "removed"]
    replacement_refs: list[ArtifactRef]
    affected_selectors: list[ScriptSelector]
    rationale: Text


class ScriptRequestDisposition(Contract):
    request_ref: ArtifactRef
    action: Literal["still_open", "addressed", "obsolete"]
    new_selectors: list[ScriptSelector]
    rationale: Text


class DeclaredImpact(Contract):
    structure: bool
    spoken_text: bool
    evidence_basis: bool
    speaker_assignment: bool


class ScriptChangeSet(Contract):
    base_script_ref: ArtifactRef
    next_script_ref: ArtifactRef
    change_reason: Text
    operations: list[ChangeOperation]
    identity_map: list[IdentityMapping]
    research_request_dispositions: list[ScriptRequestDisposition]
    dependency_dispositions: list[AuthorDependencyProposal]
    declared_impact: DeclaredImpact


class CoveredPrefix(Contract):
    through_item_id: UUID | None
    ordered_item_ids: list[UUID]
    content_and_order_hash: Text

    @model_validator(mode="after")
    def through(self):
        if self.through_item_id != (self.ordered_item_ids[-1] if self.ordered_item_ids else None):
            raise ValueError("prefix_through_mismatch")
        return self


class AudienceAssumption(Contract):
    assumption: Text
    basis: Literal["episode_spec"]


class IntroducedEntity(Contract):
    entity: Text
    aliases: list[str]
    introduction_selectors: list[ScriptSelector]


class ExplainedConcept(Contract):
    concept: Text
    explanation_summary: Text
    supporting_selectors: list[ScriptSelector]
    depth: Text


class EstablishedConnection(Contract):
    connection: Text
    supporting_selectors: list[ScriptSelector]
    qualification: str


class QuestionPromise(Contract):
    statement: Text
    opened_at: list[ScriptSelector]
    status: Literal["open", "partly_answered", "answered", "explicitly_unknown"]
    addressed_at: list[ScriptSelector]
    rationale: Text


class ComprehensionRisk(Contract):
    description: Text
    selectors: list[ScriptSelector]
    proposed_action: Text


class ListenerState(Contract):
    script_ref: ArtifactRef
    covered_prefix: CoveredPrefix
    audience_assumptions: list[AudienceAssumption]
    introduced_entities: list[IntroducedEntity]
    explained_concepts: list[ExplainedConcept]
    established_connections: list[EstablishedConnection]
    questions_and_promises: list[QuestionPromise]
    comprehension_risks: list[ComprehensionRisk]
    extractor_profile_ref: ArtifactRef
