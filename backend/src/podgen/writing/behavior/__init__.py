"""Behavior policies with editable production prompts and immutable historical releases."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from podgen.domain.errors import ContractError
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import RoleInstructions, load_role
from podgen.writing.prompts.composition import read

ProfileName = Literal[
    "production/1",
    "existing/1",
    "narrative-rebuild/1",
    "narrative-rebuild/2",
    "editorial-candidate/1",
    "editorial-candidate/2",
]
PRODUCTION: ProfileName = "production/1"
DEFAULT: ProfileName = PRODUCTION
CANDIDATES = ("editorial-candidate/1", "editorial-candidate/2")
NARRATIVES = ("narrative-rebuild/1", "narrative-rebuild/2")
NAMES = (PRODUCTION, "existing/1", *NARRATIVES, *CANDIDATES)
ROOT = files(__name__)


@dataclass(frozen=True)
class BehaviorProfile:
    name: ProfileName
    editorial_pass: int
    resources: Mapping
    planning_contract: str | None
    planning_duration: str | None
    selection: str | None
    revision_context: str | None
    reader_schema: str
    reading_audience_from_brief: bool
    projection: str = V9
    prompt_source: Literal["current", "history"] = "history"

    @property
    def fact_context(self):
        from podgen.writing.fact_context import SIGNATURE_VERSION

        return SIGNATURE_VERSION if self.name == PRODUCTION else None

    @property
    def fact_schema(self):
        from podgen.writing.revision.contracts import FACT_VERSION

        return "fact-revision/5" if self.name == PRODUCTION else FACT_VERSION

    @property
    def reading_audit(self):
        from podgen.writing.reading_audit import VERSION

        return VERSION if self.name == PRODUCTION else None

    @property
    def prompt_cache_layout(self):
        from podgen.writing.prompt_cache import VERSION

        return VERSION if self.name == PRODUCTION else None

    @property
    def planning_ending_guidance(self):
        from podgen.writing.ending_guidance import VERSION

        return VERSION if self.name == PRODUCTION else None

    @property
    def author_source_excerpts(self):
        from podgen.writing.source_passages import VERSION

        return VERSION if self.name == PRODUCTION else None

    @property
    def author_plan_handoff(self):
        return "approved-story/1" if self.name == PRODUCTION else None

    @property
    def plan_review_schema(self):
        from podgen.writing.plan_review_descriptions import PRESERVE_VERSION

        return PRESERVE_VERSION if self.name in ("narrative-rebuild/2", PRODUCTION) else None

    @property
    def verification(self):
        if self.name == PRODUCTION:
            return "editorial-recheck/3"
        return "editorial-recheck/1" if self.name in CANDIDATES else None

    def instructions(self, role, format, *, sources="cards"):
        if self.prompt_source == "current":
            return load_role(role, format, sources=sources)
        if role in ("anchor-repair", "plan-target-repair"):
            entry = self.resources["repairs"][role]
        elif role in (
            "reader-review",
            "fact-review",
            "dual-revise",
            "revision-verification",
            "fact-verification",
        ):
            key = f"reader-review-{self.editorial_pass}" if role == "reader-review" else role
            entry = self.resources["readings"][key]
        else:
            entry = self.resources["formats"][format][role]
        return RoleInstructions(entry["version"], _body(entry))

    def craft(self, language):
        if language.lower().replace("_", "-").split("-")[0] != "ru":
            return None
        if self.prompt_source == "current":
            text = read("style-reference-ru.md")
            return dict(language="ru", text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
        entry = self.resources["craft_ru"]
        return dict(language="ru", text=_body(entry), sha256=entry["sha256"])

    def audience(self, assignment):
        return (
            (assignment.listener_profile or assignment.audience)
            if self.reading_audience_from_brief
            else (assignment.listener_profile or "General listeners; no prior topic knowledge is assumed.")
        )

    def validate_model(self, stage, profile):
        if self.name == "existing/1":
            return
        from podgen.runtime.evaluation.reading_workflow import base_stage

        kind = base_stage(stage)
        model = {
            "draft": "claude-opus-5-5",
            "revise": "claude-opus-5-5",
            "plan": "gpt-6.1-sol",
            "plan-revise": "gpt-6.1-sol",
        }.get(kind, "gpt-6.1-terra")
        if self.name == PRODUCTION:
            from podgen.runtime.evaluation.compiler import validate_profile

            provider = "anthropic" if model == "claude-opus-5-5" else "openai"
            if profile.provider != provider:
                raise ContractError("behavior_model_profile_mismatch", stage)
            validation_stage = {"anchor-repair": "review", "plan-target-repair": "plan-review"}.get(
                stage, stage
            )
            validate_profile(profile, validation_stage)
        if (
            profile.model_revision_or_alias != model
            or profile.output_limit != 24576
            or profile.context_limit != 200000
            or profile.generation_parameters
            != {
                "reasoning": {"effort": "medium"},
                "text": {"verbosity": "high"},
                "store": False,
                "background": False,
            }
        ):
            raise ContractError("behavior_model_profile_mismatch", stage)


def _body(entry):
    raw = read(entry["path"], historical=True).encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ContractError("released_writing_asset_hash_mismatch")
    return raw.decode("utf-8")


def resolve_behavior(name=None, editorial_pass=1):
    name = DEFAULT if name is None else name
    if (
        name not in NAMES
        or editorial_pass not in (1, 2, 3)
        or name in ("existing/1", PRODUCTION)
        and editorial_pass != 1
    ):
        raise ContractError("unknown_writing_behavior")
    modern = name == "existing/1"
    if name == PRODUCTION:
        from .resources import CurrentResources

        resources = CurrentResources()
    else:
        catalog = ("existing-1" if modern else "narrative-rebuild-1") + ".json"
        from .resources import ReleasedResources

        overlays = CANDIDATES[: CANDIDATES.index(name) + 1] if name in CANDIDATES else ()
        if name == "narrative-rebuild/2":
            overlays = (name,)
        resources = ReleasedResources(
            catalog, tuple(release.replace("/", "-") + ".json" for release in overlays)
        )
    return BehaviorProfile(
        name,
        editorial_pass,
        resources,
        "planning-scene-development/1"
        if name in CANDIDATES
        else "planning-story-choice/1"
        if modern
        else None,
        "planning-duration-context/1" if modern else None,
        "plan-material-selection/2" if modern else None,
        "script-edit-assignment/3" if name == PRODUCTION else "script-edit-assignment/1" if modern else None,
        "listener-experience/14"
        if name == PRODUCTION
        else "listener-experience/6"
        if modern
        else "listener-experience/5",
        modern,
        prompt_source="current" if name == PRODUCTION else "history",
    )
