from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from podgen.domain.contracts.common import ArtifactRef, Contract, Text


class GeminiProfile(Contract):
    provider: Literal["gemini"] = "gemini"
    agent: Literal["deep-research-preview-04-2026", "deep-research-max-preview-04-2026"]
    api_revision: Literal["2026-05-20"] = "2026-05-20"
    credential_env: Text = "GEMINI_API_KEY"
    request_timeout_seconds: float = Field(default=120, ge=1, le=600)


class ResearchConfig(Contract):
    contract: Literal["hosted-research/1"] = "hosted-research/1"
    run_id: UUID
    # Preserve the complete commission, including provider-independent editorial metadata.
    assignment: dict[str, JsonValue]
    instructions: Text
    profile: GeminiProfile
    episode_spec_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def valid_assignment(self):
        for name in ("user_request", "output_language"):
            if not isinstance(self.assignment.get(name), str) or not self.assignment[name].strip():
                raise ValueError("hosted_research_assignment_requires_" + name)
        scope = self.assignment.get("scope")
        if not isinstance(scope, dict) or scope.get("source_mode") != "open_web":
            raise ValueError("hosted_research_requires_explicit_open_web_scope")
        return self


class Approval(Contract):
    request_hash: Text
    reason: Text
    # The provider determines work performed within one job. This is not a USD cap.
    accepts_variable_provider_cost: Literal[True]
    max_submissions: Literal[1] = 1


TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "incomplete",
    "requires_action",
    "requires_continuation",
}
