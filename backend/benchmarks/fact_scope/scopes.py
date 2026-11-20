"""Validated task boundaries and compact, source-bound handoff to the final read."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from podgen.domain.canonical import digest
from podgen.writing.reading_audit.schemas import AuditedFact
from podgen.writing.roles import FullText


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Section(Strict):
    subject: FullText
    last_utterance_index: int = Field(ge=0)


class Partition(Strict):
    sections: list[Section] = Field(min_length=2)

    def ranges(self, count):
        start = 0
        for section in self.sections:
            stop = section.last_utterance_index + 1
            if not start < stop <= count:
                raise ValueError("invalid_section_boundary")
            yield start, stop
            start = stop
        if start != count:
            raise ValueError("incomplete_section_coverage")


class Disposition(Strict):
    issue_id: FullText
    outcome: Literal["confirmed", "resolved", "still_open"]
    decision_id: FullText
    reason: FullText


class Synthesis(AuditedFact):
    fragment_dispositions: list[Disposition]


PARTITION = """Divide this podcast into contiguous reporting assignments for independent fact readers.
Keep each scene, explanation and chain of inference intact where possible. Choose the fewest
sections that give each reader a distinct, coherent investigation. Cover the entire script in
order, including transitions and the ending. Return each section's subject and inclusive last
zero-based utterance index. Do not assess the accuracy of the script."""

METHOD = """This is a controlled reading of a fixed source library. Every reader has access to the
same saved documents via search_sources, list_sources and read_source. No new documents can be
acquired in this comparison. Previously read passages supplied under previous_evidence have
been checked against those documents; cite their offered H identifiers as previous evidence.
An unavailable source is a gap, never evidence against the speaker. Give a bounded editorial
decision on the evidence actually available."""

FOCUS = """The script field is your reporting assignment and has local zero-based indices. The
episode_context field supplies the entire episode for interpreting references. Check the assigned
script, including its connections to the rest of the story. Use only the assignment's local
utterance indices for anchors and coverage."""

SYNTHESIS = """You have the whole episode and independent section reports. Produce one actionable
factual review for the writer. Check relationships across sections: attribution, chronology,
comparisons, causal inference and changing descriptions of the same event or person. Carry
grounded local findings into the review once. Reopen a local check only for a concrete conflict
or missing link. Account once for each incoming issue_id in fragment_dispositions. Link a
confirmed issue to the current decision behind an important or blocking finding. Link a resolved
issue to a supported decision with evidence and give the reason. Use still_open for an
unverified question that remains explicitly open in the review. Use the global script indices."""


def handoff(reports):
    proofs, aliases, sections, issues = {}, {}, [], {}
    for start, record in reports:
        audit = record["output"]
        evidence = record["reading_audit"]["resolved_evidence"]

        def remap(ref, evidence=evidence):
            raw = evidence[ref["from_review"] + ":" + ref["evidence_id"]]
            key = digest(raw["source_selectors"])
            if key not in aliases:
                alias = f"H{len(proofs) + 1}"
                proofs[alias], aliases[key] = raw, alias
            return aliases[key]

        def anchors(rows, start=start):
            return [dict(a, utterance_index=start + a["utterance_index"]) for a in rows]

        decisions = [
            dict(
                id=d["decision_id"],
                anchors=anchors(d["anchors"]),
                meaning=d["contextual_meaning"],
                verdict=d["verdict"],
                sources=[remap(r) for r in d["source_refs"]],
            )
            for d in audit["audit"]["claim_checks"]
        ]
        findings = []
        for i, finding in enumerate(audit["findings"]):
            if finding["priority"] == "optional":
                continue
            key = f"U{start + 1}F{i + 1}"
            issues[key] = anchors(finding["anchors"])
            findings.append(
                dict(
                    issue_id=key,
                    anchors=issues[key],
                    observation=finding["observation"],
                    requested_change=finding["requested_change"],
                    sources=[remap(ref) for ref in record["finding_passages"][i]],
                    decisions=next(
                        row["decision_ids"] for row in audit["finding_links"] if row["finding_index"] == i
                    ),
                )
            )
        pending = []
        for row in decisions:
            if row["verdict"] == "unchecked":
                key = f"U{start + 1}{row['id']}"
                issues[key] = row["anchors"]
                pending.append(dict(issue_id=key, anchors=issues[key], question=row["meaning"]))
        for i, question in enumerate(audit["research_requests"]):
            key = f"U{start + 1}Q{i + 1}"
            issues[key] = anchors(question["anchors"])
            pending.append(dict(issue_id=key, anchors=issues[key], question=question["question"]))
        sections.append(
            dict(
                first_utterance_index=start,
                decisions=decisions,
                findings=findings,
                open_questions=pending,
                limitations=audit["important_limits"],
            )
        )
    return sections, proofs, issues


def validate_dispositions(output, issues):
    dispositions = output["fragment_dispositions"]
    if sorted(d["issue_id"] for d in dispositions) != sorted(issues):
        raise ValueError("missing_or_duplicate_section_issue")
    decisions = {d["decision_id"]: d for d in output["audit"]["claim_checks"]}
    linked = {
        d
        for link in output["finding_links"]
        if output["findings"][link["finding_index"]]["priority"] in ("important", "blocking")
        for d in link["decision_ids"]
    }
    for row in dispositions:
        decision = decisions.get(row["decision_id"])
        if decision is None or not (
            {a["utterance_index"] for a in decision["anchors"]}
            & {a["utterance_index"] for a in issues[row["issue_id"]]}
        ):
            raise ValueError("unrelated_section_disposition")
        if row["outcome"] == "confirmed" and row["decision_id"] not in linked:
            raise ValueError("unresolved_section_issue")
        if row["outcome"] == "still_open" and (
            decision["verdict"] != "unchecked" or output["recommendation"] == "keep"
        ):
            raise ValueError("unreported_open_question")
        if row["outcome"] == "resolved" and (
            decision["verdict"] != "supported" or not decision["source_refs"]
        ):
            raise ValueError("unsubstantiated_section_resolution")
