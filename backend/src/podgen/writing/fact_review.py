"""Fact findings cite the compact support needed by a subsequent writer."""

from pydantic import Field

from podgen.domain.errors import ContractError

from .roles import EditorialFeedback, EditorialFinding, FullText


class FactFinding(EditorialFinding):
    support_labels: list[FullText] = Field(
        description="Exact supplied support labels needed for this finding and repair; empty if unsupported."
    )


class FactFeedback(EditorialFeedback):
    findings: list[FactFinding]


def feedback_model(version=None):
    if version is None:
        return FactFeedback
    from .fact_context import SCHEMA_VERSION
    from .revision.contracts import FACT_V1, FACT_VERSION

    if version not in (FACT_V1, FACT_VERSION, SCHEMA_VERSION):
        raise ContractError("unknown_fact_review_schema_version")
    from .revision.scopes import FactAssessment, FactAssessmentV2

    return FactAssessment if version == FACT_V1 else FactAssessmentV2


def snapshot_model(value):
    from .revision.contracts import FACT_V1, FACT_VERSION

    if "revision_scope" not in value:
        return FactFeedback
    scope = value["revision_scope"]
    return feedback_model(
        FACT_V1 if scope["scale"] == "sequence" and "sequence_ranges" not in scope else FACT_VERSION
    )


def describe_schema(schema, stage, version):
    from .fact_context import SCHEMA_VERSION
    from .revision.contracts import FACT_VERSION

    if stage == "fact-review" and version in (FACT_VERSION, SCHEMA_VERSION):
        from .fact_review_descriptions import describe_fact_review

        describe_fact_review(schema)
        if version == SCHEMA_VERSION:
            from .fact_claim_descriptions import describe_fact_claims

            describe_fact_claims(schema)
    return schema
