"""Delta wire reports expand into the existing complete, evidence-bound fact audit."""

from typing import Literal

from pydantic import Field, field_validator

from ..roles import FullText, RoleOutput
from .schemas import AuditedFact, DecisionDisposition


class RetainedConfirmation(RoleOutput):
    confirmed: Literal[True] = Field(
        description="Confirm that every previous decision not explicitly updated still applies in the full current context, with its recorded meaning, verdict and evidence."
    )
    grounds: FullText = Field(
        description="One explicit account of why the remaining decisions still apply, including dependent conclusions and changed surrounding context. This is a factual judgment, not a count or a default."
    )

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_boolean_confirmation(cls, value):
        if value is not True:
            raise ValueError("audit_delta_confirmation_missing")
        return value


class ChangedDecision(DecisionDisposition):
    disposition: Literal["reassessed", "retired"]


class FactRecheck(AuditedFact):
    retained_confirmation: RetainedConfirmation
    previous_dispositions: list[ChangedDecision] = Field(
        description="Only previous decisions changed, split, merged or retired. Address every requires_update decision. Other decisions are confirmed once, not rewritten."
    )


def schema():
    from .consistency import DELTA_VERSION, describe
    from .descriptions import DEFINITIONS, describe_fact

    result = FactRecheck.model_json_schema()
    describe_fact(result)
    for key, description in DEFINITIONS["DecisionDisposition"].items():
        result["$defs"]["ChangedDecision"]["properties"][key]["description"] = description
    describe(result, DELTA_VERSION)
    result["properties"]["previous_dispositions"]["description"] = (
        "Only previous decisions changed, split, merged or retired. Address every requires_update decision. Confirm the other decisions once in retained_confirmation; do not rewrite them."
    )
    result["$defs"]["ChangedDecision"]["properties"]["disposition"]["description"] = (
        "Reassessed explicitly replaces this prior decision with the listed current decisions. Retired removes it with concrete grounds and no current targets. Never infer evidence inheritance across split or merged decisions."
    )
    result["$defs"]["FactAudit"]["properties"]["claim_checks"]["description"] = (
        "Only new or explicitly reassessed current decisions. Do not rewrite unchanged decisions. Each new/split/merged claim needs its own exact current anchors and explicitly selected evidence; prior evidence is never copied into these rows automatically."
    )
    result["$defs"]["FactAudit"]["description"] = (
        "Full current coverage and factual judgment, with only new or reassessed claim rows. The other prior decisions are confirmed once and retained by the backend."
    )
    result["properties"]["finding_links"]["description"] += (
        " Current decision IDs include confirmed previous decisions as well as the new or reassessed rows supplied here."
    )
    return result
