"""Exact, server-enumerated plan addresses; repair never edits an editorial judgment."""

from dataclasses import dataclass

from pydantic import Field

from podgen.domain.errors import ContractError

from .planning_roles import PlanningOutput, PlanReviewOutput
from .roles import FullText, RoleOutput


class PlanTargetChoice(RoleOutput):
    target_id: FullText
    selected_path: FullText | None = Field(
        description="An exact offered path, or null when none supports the original judgment."
    )


class PlanTargetChoices(RoleOutput):
    choices: list[PlanTargetChoice] = Field(min_length=1)


def allowed_review_targets(candidate):
    paths = []

    def walk(value, prefix=""):
        if isinstance(value, dict):
            children = ((key.replace("~", "~0").replace("/", "~1"), child) for key, child in value.items())
        elif isinstance(value, list):
            children = ((str(index), child) for index, child in enumerate(value))
        else:
            return
        for key, child in children:
            path = prefix + "/" + key
            paths.append(path)
            walk(child, path)

    walk(candidate)
    return paths


@dataclass(frozen=True)
class InvalidTarget:
    group: str
    entry: int
    index: int

    @property
    def target_id(self):
        return f"{self.group}/{self.entry}/targets/{self.index}"


@dataclass(frozen=True)
class PlanTargetRepair:
    candidate: PlanningOutput
    review: PlanReviewOutput
    material_catalog: tuple[str, ...]
    allowed: tuple[str, ...]
    invalid: tuple[InvalidTarget, ...]

    @classmethod
    def build(cls, candidate, review, material_catalog):
        review.validate_context(candidate, material_locators=material_catalog)
        data = review.model_dump(mode="json")
        allowed = tuple(allowed_review_targets(candidate.model_dump(mode="json")))
        allowed_set = set(allowed)
        invalid = tuple(
            InvalidTarget(group, i, j)
            for group in ("strengths", "findings")
            for i, entry in enumerate(data[group])
            for j, path in enumerate(entry["targets"])
            if path not in allowed_set
        )
        return cls(
            candidate.model_copy(deep=True),
            review.model_copy(deep=True),
            tuple(material_catalog),
            allowed,
            invalid,
        )

    def as_payload(self):
        rows = []
        for target in self.invalid:
            entry = getattr(self.review, target.group)[target.entry]
            rows.append(
                dict(
                    target_id=target.target_id,
                    rejected_pointer=entry.targets[target.index],
                    editorial_judgment=entry.model_dump(mode="json", exclude={"targets"}),
                )
            )
        return dict(invalid_targets=rows, allowed_review_targets=list(self.allowed))

    def apply(self, choices):
        locations = {item.target_id: item for item in self.invalid}
        ids = [choice.target_id for choice in choices.choices]
        if len(ids) != len(locations) or set(ids) != locations.keys():
            raise ContractError("plan_target_repair_target_mismatch")
        allowed = set(self.allowed)
        result = self.review.model_copy(deep=True)
        for choice in choices.choices:
            if choice.selected_path is None:
                raise ContractError("unresolved_plan_review_target")
            if choice.selected_path not in allowed:
                raise ContractError("unknown_plan_review_target_choice")
            target = locations[choice.target_id]
            getattr(result, target.group)[target.entry].targets[target.index] = choice.selected_path
        result.validate_against(self.candidate, material_locators=self.material_catalog)
        return result
