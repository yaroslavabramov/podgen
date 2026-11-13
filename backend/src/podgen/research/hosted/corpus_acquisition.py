"""Acquire the complete reported corpus, retaining URL aliases and reusable outcomes."""

from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid5

from pydantic import Field

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, Contract
from podgen.domain.contracts.evidence import SourceRights
from podgen.domain.errors import ContractError
from podgen.sources.acquisition.contracts import AcquisitionRequest, AcquisitionResult
from podgen.sources.acquisition.service import acquire_source


class CorpusAcquisition(Contract):
    profile_ref: ArtifactRef
    rights: SourceRights
    reuse_selection_ids: list[UUID] = Field(default_factory=list)


def document_url(url):
    """Only normalize transport-equivalent parts; preserve path, query and their case."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ContractError("research_invalid_source_url")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port and (parsed.scheme, port) not in (("http", 80), ("https", 443)):
        host += ":" + str(port)
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def reusable_outcomes(journal, config):
    result = {}
    for selection_id in config.reuse_selection_ids:
        root = f"source-acquisition:{selection_id}"
        selection, previous = journal.get(root + ":selection"), journal.get(root + ":result")
        if not selection or not previous:
            raise ContractError("research_missing_reusable_source_selection")
        if selection["report_ref"] != journal.ref("result").model_dump(mode="json"):
            raise ContractError("research_reusable_selection_report_mismatch")
        settings = selection["selection"]
        if settings["profile_ref"] != config.profile_ref.model_dump(mode="json") or settings[
            "rights"
        ] != config.rights.model_dump(mode="json"):
            raise ContractError("research_reusable_selection_policy_mismatch")
        for row in previous["sources"]:
            if row["report_ref"] != selection["report_ref"]:
                raise ContractError("research_reusable_selection_report_mismatch")
            try:
                key = document_url(row["reported_url"])
            except (ValueError, ContractError):
                key = row["reported_url"]
            prior = result.get(key)
            # Do not replace a retained readable version with a failed later attempt.
            if prior is None or (
                prior["status"] != "available" and row["acquisition"]["status"] == "available"
            ):
                result[key] = row["acquisition"]
    return result


async def acquire_corpus(journal, operation_id, config, *, acquire=acquire_source):
    config = CorpusAcquisition.model_validate(config)
    with journal.lock():
        report = journal.get("result")
        if not report or report.get("status") != "completed":
            raise ContractError("hosted_research_sources_require_completed_report")
        reported = report["material"]["reported_sources"]
        urls = list(dict.fromkeys(item["url"] for item in reported))
        root = f"source-acquisition:{operation_id}"
        journal.put(
            root + ":selection",
            dict(
                contract="complete-research-corpus/1",
                selection=dict(operation_id=str(operation_id), **config.model_dump(mode="json")),
                urls=urls,
                report_ref=journal.ref("result").model_dump(mode="json"),
            ),
        )
        previous = journal.get(root + ":result")
        if previous:
            return previous
        reusable = reusable_outcomes(journal, config)
        outputs = []
        for url in urls:
            invalid = False
            try:
                canonical = document_url(url)
            except (ValueError, ContractError):
                # A malformed reported link is an individual failed acquisition,
                # not permission to skip the rest of the research corpus.
                canonical, invalid = url, True
            key = root + ":document:" + digest(canonical)
            outcome = journal.get(key)
            if outcome is None:
                outcome = reusable.get(canonical)
                if outcome is None and invalid:
                    request_ref = journal.put(key + ":request", dict(reported_url=url))
                    outcome = AcquisitionResult(
                        acquisition_record_ref=request_ref,
                        sources=[],
                        status="failed",
                        diagnostics=["invalid_reported_source_url"],
                    ).model_dump(mode="json")
                if outcome is None:
                    request = AcquisitionRequest(
                        operation_id=uuid5(operation_id, canonical),
                        source_id=uuid5(journal.project, "reported-source:" + canonical),
                        url=canonical,
                        profile_ref=config.profile_ref,
                        rights=config.rights,
                    )
                    outcome = (await acquire(journal.repo, journal.project, request)).model_dump(mode="json")
                journal.put(key, outcome)
            outputs.append(
                dict(
                    reported_url=url,
                    document_url=canonical,
                    report_ref=journal.ref("result").model_dump(mode="json"),
                    reported_occurrences=[item for item in reported if item["url"] == url],
                    acquisition=outcome,
                    evidence_status="no_exact_citation_binding",
                    version_relationship="later_fetch_not_proof_of_provider_read_bytes",
                )
            )
        result = dict(
            contract="hosted-research-sources/2",
            selection_ref=journal.ref(root + ":selection").model_dump(mode="json"),
            sources=outputs,
        )
        journal.put(root + ":result", result)
        return result
