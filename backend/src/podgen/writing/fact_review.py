"""Fact findings cite the compact support needed by a subsequent writer."""

from pydantic import Field

from podgen.domain.errors import ContractError

from .roles import EditorialFeedback, EditorialFinding, FullText


class FactFinding(EditorialFinding):
    support_labels: list[FullText] = Field(
        description="Exact supplied support labels needed for this finding and repair; empty if unsupported."
    )


class SourcedFactFinding(FactFinding):
    source_read_ids: list[FullText] = Field(
        description="Exact E identifiers returned by read_source in this review that support this specific finding. Empty when existing evidence suffices."
    )


class FactFeedback(EditorialFeedback):
    findings: list[FactFinding]


def feedback_model(version=None):
    if version == "fact-revision/5":
        from .reading_audit.schemas import AuditedFact

        return AuditedFact
    if version is None:
        return FactFeedback
    from .fact_context import READ_SCHEMA_VERSION, SCHEMA_VERSION
    from .revision.contracts import FACT_V1, FACT_VERSION

    if version == READ_SCHEMA_VERSION:
        from .revision.scopes import SourcedFactAssessment

        return SourcedFactAssessment
    if version not in (FACT_V1, FACT_VERSION, SCHEMA_VERSION):
        raise ContractError("unknown_fact_review_schema_version")
    from .revision.scopes import FactAssessment, FactAssessmentV2

    return FactAssessment if version == FACT_V1 else FactAssessmentV2


def snapshot_model(value):
    if "audit" in value:
        return feedback_model("fact-revision/5")
    from .revision.contracts import FACT_V1, FACT_VERSION

    if "revision_scope" not in value:
        return FactFeedback
    if any("source_read_ids" in row for row in value["findings"]):
        from .fact_context import READ_SCHEMA_VERSION

        return feedback_model(READ_SCHEMA_VERSION)
    scope = value["revision_scope"]
    return feedback_model(
        FACT_V1 if scope["scale"] == "sequence" and "sequence_ranges" not in scope else FACT_VERSION
    )


def describe_schema(schema, stage, version):
    if stage == "fact-review" and version == "fact-revision/5":
        from .reading_audit.descriptions import describe_fact

        describe_fact(schema)
        return schema
    from .fact_context import READ_SCHEMA_VERSION, SCHEMA_VERSION
    from .revision.contracts import FACT_VERSION

    if stage == "fact-review" and version in (FACT_VERSION, SCHEMA_VERSION, READ_SCHEMA_VERSION):
        from .fact_review_descriptions import describe_fact_review

        sourced = version == READ_SCHEMA_VERSION
        finding = "SourcedFactFinding" if sourced else "FactFinding"
        describe_fact_review(schema, finding=finding)
        if version in (SCHEMA_VERSION, READ_SCHEMA_VERSION):
            from .fact_claim_descriptions import describe_fact_claims

            describe_fact_claims(schema, finding_name=finding)
    return schema
