"""The editor revisits an assignment against the actual before and after."""

from copy import deepcopy
from difflib import SequenceMatcher
from typing import Literal

from pydantic import Field

from podgen.domain.errors import ContractError

from ..roles import FullText, RoleOutput
from .scopes import ReaderAssessmentV4

KEY = "revision_verification_contract"
VERSION = "editorial-recheck/1"
STAGES = ("reader-review-1", "reader-review-2")


def applies(stage, version):
    if version not in (None, VERSION):
        raise ContractError("unknown_revision_verification_contract")
    return version == VERSION and stage in STAGES


class AssignmentResult(RoleOutput):
    assignment_id: FullText
    outcome: Literal["achieved", "solved_differently", "no_longer_needed", "still_open"]
    judgment: FullText = Field(description="What the edit actually did here and how that reads now.")
    followup_finding: int | None = Field(
        ge=0, description="Zero-based current finding for an outstanding issue; otherwise null."
    )


class RevisionReading(ReaderAssessmentV4):
    reading_mode: Literal["informed"]
    assignment_results: list[AssignmentResult]
    effect_on_episode: FullText = Field(
        description="How the edit changed the complete listening experience, including any loss worth repairing."
    )

    def verify_assignments(self, comparison):
        expected = [row["id"] for row in comparison["assignments"]]
        actual = [row.assignment_id for row in self.assignment_results]
        if sorted(actual) != sorted(expected):
            raise ValueError("revision_verification_assignments_mismatch")
        for row in self.assignment_results:
            index = row.followup_finding
            if (row.outcome == "still_open") != (index is not None) or (
                index is not None and index >= len(self.findings)
            ):
                raise ValueError("revision_verification_followup_mismatch")


def schema():
    from ..reader_schema import VERSION as READER_VERSION
    from ..reader_schema import describe_reader

    result = RevisionReading.model_json_schema()
    extra = {name: result["properties"][name] for name in ("assignment_results", "effect_on_episode")}
    describe_reader(result, READER_VERSION)
    props = result["properties"]
    result["properties"] = {
        "reading_mode": props.pop("reading_mode"),
        "listener_answer": props.pop("listener_answer"),
        **extra,
        **props,
    }
    result["required"] = list(result["properties"])
    return result


def bind_assignments(template, comparison):
    result = deepcopy(template)
    ids = [order["id"] for order in comparison["assignments"]]
    field = result["properties"]["assignment_results"]
    field.update(minItems=len(ids), maxItems=len(ids))
    if ids:
        result["$defs"]["AssignmentResult"]["properties"]["assignment_id"]["enum"] = ids
    return result


def previous_utterances(previous, current):
    """Reconstruct the old script in order, sharing identical turns from the complete current script."""

    def keys(script):
        return [(row["speaker"], row["text"]) for row in script["utterances"]]

    blocks = []
    matcher = SequenceMatcher(None, keys(previous), keys(current), autojunk=False)
    for kind, i, j, a, b in matcher.get_opcodes():
        if kind == "equal":
            blocks.append(dict(copy_current=dict(first=a, last=b - 1)))
        elif i != j:
            blocks.append(dict(previous_turns=deepcopy(previous["utterances"][i:j])))
    return blocks


def comparison(previous, current, guidance):
    scopes, assignments = {}, []
    for review in guidance:
        origin = review["origin"]
        prefix = {"reader": "R", "fact": "F"}[origin]
        scope = review["revision_scope"]
        if scope["scale"] != "none":
            scopes[origin] = deepcopy(scope)
        for i, finding in enumerate(review["findings"], 1):
            assignments.append(
                dict(
                    id=f"{prefix}{i}",
                    **{
                        key: deepcopy(finding[key])
                        for key in ("anchors", "priority", "observation", "requested_change", "preserve")
                    },
                )
            )
        if scope["scale"] in ("sequence", "whole_episode") or (
            scope["scale"] != "none" and not review["findings"]
        ):
            assignments.append(dict(id=f"{prefix}0", scope=origin))
    return dict(
        previous_script_in_order=previous_utterances(previous, current),
        scopes=scopes,
        assignments=assignments,
    )
