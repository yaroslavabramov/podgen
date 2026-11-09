"""Released writing behavior. Prompt bodies are shared, addressed by their exact UTF-8 bytes."""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from podgen.domain.errors import ContractError
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import RoleInstructions

ProfileName = Literal[
    "existing/1",
    "narrative-rebuild/1",
    "narrative-rebuild/2",
    "editorial-candidate/1",
    "editorial-candidate/2",
]
DEFAULT: ProfileName = "existing/1"
CANDIDATES = ("editorial-candidate/1", "editorial-candidate/2")
NARRATIVES = ("narrative-rebuild/1", "narrative-rebuild/2")
NAMES = ("existing/1", *NARRATIVES, *CANDIDATES)
ROOT = files(__name__)


@dataclass(frozen=True)
class BehaviorProfile:
    name: ProfileName
    editorial_pass: int
    resources: dict
    planning_contract: str | None
    planning_duration: str | None
    selection: str | None
    revision_context: str | None
    reader_schema: str
    reading_audience_from_brief: bool
    projection: str = V9

    @property
    def plan_review_schema(self):
        from podgen.writing.plan_review_descriptions import PRESERVE_VERSION

        return PRESERVE_VERSION if self.name == "narrative-rebuild/2" else None

    @property
    def verification(self):
        return "editorial-recheck/1" if self.name in CANDIDATES else None

    def instructions(self, role, format):
        if role in ("anchor-repair", "plan-target-repair"):
            entry = self.resources["repairs"][role]
        elif role in ("reader-review", "fact-review", "dual-revise", "revision-verification"):
            key = f"reader-review-{self.editorial_pass}" if role == "reader-review" else role
            entry = self.resources["readings"][key]
        else:
            entry = self.resources["formats"][format][role]
        return RoleInstructions(entry["version"], _body(entry["sha256"]))

    def craft(self, language):
        if language.lower().replace("_", "-").split("-")[0] != "ru":
            return None
        identity = self.resources["craft_ru"]
        return dict(language="ru", text=_body(identity), sha256=identity)

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


@lru_cache
def _body(identity):
    raw = ROOT.joinpath("assets", identity + ".md").read_bytes()
    if hashlib.sha256(raw).hexdigest() != identity:
        raise ContractError("released_writing_asset_hash_mismatch")
    return raw.decode()


@lru_cache
def resolve_behavior(name=None, editorial_pass=1):
    name = DEFAULT if name is None else name
    if name not in NAMES or editorial_pass not in (1, 2, 3) or name == "existing/1" and editorial_pass != 1:
        raise ContractError("unknown_writing_behavior")
    modern = name == "existing/1"
    catalog = ("existing-1" if modern else "narrative-rebuild-1") + ".json"
    resources = json.loads(ROOT.joinpath(catalog).read_text(encoding="utf-8"))
    overlays = CANDIDATES[: CANDIDATES.index(name) + 1] if name in CANDIDATES else ()
    if name == "narrative-rebuild/2":
        overlays = (name,)
    for release in overlays:
        # Each overlay inherits the preceding release and replaces only its owned roles.
        overlay = ROOT.joinpath(release.replace("/", "-") + ".json")
        if not overlay.is_file():
            raise ContractError("writing_behavior_not_released", name)
        change = json.loads(overlay.read_text(encoding="utf-8"))
        for fmt, roles in change["formats"].items():
            resources["formats"][fmt].update(roles)
        resources["readings"].update(change["readings"])
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
        "script-edit-assignment/1" if modern else None,
        "listener-experience/6" if modern else "listener-experience/5",
        modern,
    )
