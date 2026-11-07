"""Repair a review by selecting exact passages, without regenerating its editorial judgments."""

import re
from dataclasses import dataclass

from pydantic import Field

from podgen.domain.errors import ContractError

from .roles import EditorialFeedback, FullText, RoleOutput, UtteranceAnchor, WriteOutput


class AnchorChoice(RoleOutput):
    anchor_id: FullText
    selector_id: FullText | None = Field(description="An offered selector ID, or null if no passage fits.")


class AnchorChoices(RoleOutput):
    choices: list[AnchorChoice] = Field(min_length=1)


@dataclass(frozen=True)
class Passage:
    utterance_index: int
    start: int
    end: int
    text: str

    @property
    def selector_id(self) -> str:
        return f"u{self.utterance_index}:{self.start}-{self.end}"


def _passages(script: WriteOutput):
    for index, utterance in enumerate(script.utterances):
        text = utterance.text
        yield Passage(index, 0, len(text), text)
        start = 0
        # These boundaries offer shorter selectable ranges. The complete utterance is always available.
        for boundary in [*re.finditer(r'(?<=[.!?…])(?:[»”")]+)?(?=\s|$)', text), None]:
            end = boundary.end() if boundary is not None else len(text)
            if start < end and (start, end) != (0, len(text)):
                yield Passage(index, start, end, text[start:end])
            start = end
            while start < len(text) and text[start].isspace():
                start += 1


@dataclass(frozen=True)
class BrokenAnchor:
    group: str
    entry: int
    anchor: int

    @property
    def anchor_id(self) -> str:
        return f"{self.group}/{self.entry}/anchors/{self.anchor}"


@dataclass(frozen=True)
class AnchorRepairPlan:
    draft: WriteOutput
    feedback: EditorialFeedback
    broken: tuple[BrokenAnchor, ...]
    passages: tuple[Passage, ...]

    @classmethod
    def build(cls, draft: WriteOutput, feedback: EditorialFeedback) -> "AnchorRepairPlan":
        draft = draft.model_copy(deep=True)
        feedback = feedback.model_copy(deep=True)
        broken = []
        for group, entries in feedback.anchored_groups().items():
            for entry_index, entry in enumerate(entries):
                for anchor_index, anchor in enumerate(entry.anchors):
                    index = anchor.utterance_index
                    if index >= len(draft.utterances) or anchor.quote not in draft.utterances[index].text:
                        broken.append(BrokenAnchor(group, entry_index, anchor_index))
        return cls(draft, feedback, tuple(broken), tuple(_passages(draft)))

    def as_payload(self) -> dict:
        invalid = []
        for item in self.broken:
            entry = self.feedback.anchored_groups()[item.group][item.entry]
            invalid.append(
                {
                    "anchor_id": item.anchor_id,
                    "rejected_anchor": entry.anchors[item.anchor].model_dump(mode="json"),
                    "editorial_judgment": entry.model_dump(mode="json", exclude={"anchors"}),
                }
            )
        return {
            "invalid_anchors": invalid,
            "selectors": [
                {
                    "selector_id": passage.selector_id,
                    "speaker": self.draft.utterances[passage.utterance_index].speaker,
                    "text": passage.text,
                }
                for passage in self.passages
            ],
        }

    def apply(self, choices: AnchorChoices) -> EditorialFeedback:
        locations = {item.anchor_id: item for item in self.broken}
        selected_ids = [choice.anchor_id for choice in choices.choices]
        if len(selected_ids) != len(locations) or set(selected_ids) != locations.keys():
            raise ContractError("anchor_repair_target_mismatch")
        passages = {passage.selector_id: passage for passage in self.passages}
        repaired = self.feedback.model_copy(deep=True)
        for choice in choices.choices:
            if choice.selector_id is None:
                raise ContractError("unresolved_editorial_anchor", choice.anchor_id)
            if choice.selector_id not in passages:
                raise ContractError("unknown_anchor_selector", choice.selector_id)
            passage = passages[choice.selector_id]
            target = locations[choice.anchor_id]
            entry = repaired.anchored_groups()[target.group][target.entry]
            entry.anchors[target.anchor] = UtteranceAnchor(
                utterance_index=passage.utterance_index, quote=passage.text
            )
        repaired.validate_against(self.draft)
        return repaired
