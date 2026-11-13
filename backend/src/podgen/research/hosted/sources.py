"""Acquire reported URLs as independent, versioned documents, never as proven citations."""

from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, Contract, Text
from podgen.domain.contracts.evidence import SourceRights
from podgen.domain.errors import ContractError
from podgen.sources.acquisition.contracts import AcquisitionRequest
from podgen.sources.acquisition.service import acquire_source


class SourceSelection(Contract):
    operation_id: UUID
    profile_ref: ArtifactRef
    rights: SourceRights
    urls: list[Text] | None = None  # None selects every reported HTTP(S) URL, without a hidden limit.


async def acquire_report_sources(journal, selection, *, acquire=acquire_source):
    with journal.lock():
        report = journal.get("result")
        if not report:
            raise ContractError("hosted_research_sources_require_completed_report")
        reported = report["material"]["reported_sources"]
        available = list(dict.fromkeys(item["url"] for item in reported))
        urls = available if selection.urls is None else selection.urls
        if len(set(urls)) != len(urls) or any(url not in available for url in urls):
            raise ContractError("hosted_research_source_selection_not_in_report")
        root = "source-acquisition:" + str(selection.operation_id)
        journal.put(
            root + ":selection",
            dict(
                selection=selection.model_dump(mode="json"),
                urls=urls,
                report_ref=journal.ref("result").model_dump(mode="json"),
            ),
        )
        previous = journal.get(root + ":result")
        if previous:
            return previous
        outputs = []
        for url in urls:
            key = root + ":url:" + digest(url)
            saved = journal.get(key)
            if saved is None:
                request = AcquisitionRequest(
                    operation_id=uuid5(selection.operation_id, url),
                    source_id=uuid5(journal.project, "reported-source:" + url),
                    url=url,
                    profile_ref=selection.profile_ref,
                    rights=selection.rights,
                )
                result = await acquire(journal.repo, journal.project, request)
                saved = dict(
                    reported_url=url,
                    report_ref=journal.ref("result").model_dump(mode="json"),
                    reported_occurrences=[item for item in reported if item["url"] == url],
                    acquisition=result.model_dump(mode="json"),
                    evidence_status="no_exact_citation_binding",
                    version_relationship="later_fetch_not_proof_of_provider_read_bytes",
                )
                journal.put(key, saved)
            outputs.append(saved)
        result = dict(
            contract="hosted-research-sources/1",
            selection_ref=journal.ref(root + ":selection").model_dump(mode="json"),
            sources=outputs,
        )
        journal.put(root + ":result", result)
        return result
