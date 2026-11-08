"""Diagnostic two-desk wire; no production registration or role change."""

from typing import Literal

from pydantic import Field

from podgen.writing.reader_schema import describe_reader
from podgen.writing.revision.scopes import ReaderAssessmentV4
from podgen.writing.roles import FullText, RoleOutput, UtteranceAnchor, WriteOutput

CONTRACT = "reader-desks-diagnostic/1"


class LanguageFinding(RoleOutput):
    kind: Literal["form", "spoken_wording"]
    priority: Literal["blocking", "important", "optional"]
    anchors: list[UtteranceAnchor] = Field(min_length=1)
    observation: FullText = Field(
        description="Concrete defect in the quoted words or their combined effect in the passage."
    )
    requested_change: FullText = Field(
        description="Correction or editorial operation and its intended effect; not a rewritten script."
    )
    preserve: list[FullText] = Field(
        description="Meaning, contribution and voice to retain through the repair; may be empty."
    )


class LanguageReview(RoleOutput):
    findings: list[LanguageFinding] = Field(
        description="Addressed form and spoken-wording repairs; empty when none are needed."
    )


class ReaderDesks(RoleOutput):
    language_review: LanguageReview
    reader_review: ReaderAssessmentV4

    def validate_against(self, script: WriteOutput):
        self.reader_review.validate_against(script)
        for finding in self.language_review.findings:
            for anchor in finding.anchors:
                if anchor.utterance_index >= len(script.utterances):
                    raise ValueError("language_anchor_index_out_of_range")
                text = script.utterances[anchor.utterance_index].text
                if anchor.quote not in text:
                    raise ValueError("language_anchor_quote_mismatch")
                if text.find(anchor.quote, text.find(anchor.quote) + 1) >= 0:
                    raise ValueError("language_anchor_quote_ambiguous")
        if self.reader_review.reading_mode != "script_only":
            raise ValueError("cold_reader_required")
        if self.reader_review.research_requests:
            raise ValueError("cold_reader_has_no_external_research")

    def consistency(self):
        required = any(f.priority in ("blocking", "important") for f in self.language_review.findings)
        return not required or (
            self.reader_review.recommendation == "revise"
            and self.reader_review.revision_scope.scale != "none"
        )


def response_schema():
    schema = ReaderDesks.model_json_schema()
    describe_reader(
        dict(properties=schema["$defs"]["ReaderAssessmentV4"]["properties"], **{"$defs": schema["$defs"]}),
        "listener-experience/4",
    )
    return schema
