"""Fact findings cite the compact support needed by a subsequent writer."""

from pydantic import Field

from .roles import EditorialFeedback, EditorialFinding, FullText


class FactFinding(EditorialFinding):
    support_labels: list[FullText] = Field(
        description="Exact supplied support labels needed for this finding and repair; empty if unsupported."
    )


class FactFeedback(EditorialFeedback):
    findings: list[FactFinding]
