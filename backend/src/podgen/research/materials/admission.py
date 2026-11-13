"""Freeze the actual hosted report and acquired source versions without summarizing."""

import io
import json
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import publish
from podgen.research.hosted.journal import ResearchJournal
from podgen.writing.editorial_contract import SourceDocument

from .contracts import PERSISTED, VERSION, PrepareResearchMaterials, ResearchPacket
from .sections import SECTIONING, partition, source_mapping
from .sources import freeze_sources


def prepare_materials(repo, project, config):
    config = PrepareResearchMaterials.model_validate(config)
    journal = ResearchJournal(repo, project, config.research_run_id)
    result = journal.get("result")
    manifest = journal.get("manifest")
    if not result or result.get("status") != "completed" or not manifest:
        raise ContractError("research_materials_require_completed_report")
    report = journal.bytes("result", result["report_text_blob"]).decode("utf-8")
    mapping = source_mapping(report)
    source_results = []
    for selection_id in config.source_selection_ids:
        key = "source-acquisition:" + str(selection_id) + ":result"
        value = journal.get(key)
        if value is None:
            raise ContractError("research_materials_missing_source_selection")
        if any(
            row["report_ref"] != journal.ref("result").model_dump(mode="json") for row in value["sources"]
        ):
            raise ContractError("research_materials_sources_for_different_report")
        source_results.append((journal.ref(key), value))
    sources, documents, bindings = freeze_sources(
        repo, project, result, mapping, source_results, config.source_assessments
    )
    report_blob = BlobRef.model_validate(result["report_text_blob"])
    documents.insert(
        0,
        SourceDocument(
            key="report",
            title="Hosted research report",
            source_path="research/report.md",
            sha256=report_blob.sha256,
            purpose="dossier",
        ),
    )
    bindings["report"] = dict(
        ref=journal.ref("result").model_dump(mode="json"), blob=report_blob.model_dump(mode="json")
    )
    source_by_number = {
        source.report_number: dict(url=source.reported_url, source_id=source.source_id)
        for source in sources
        if source.report_number is not None
    }
    sections = partition(report, sources=source_by_number)
    packet = ResearchPacket(
        assignment=config.assignment,
        research_assignment=manifest["config"]["assignment"],
        documents=documents,
        sections=sections,
        sources=sources,
        report_result_ref=journal.ref("result"),
        source_result_refs=[ref for ref, _ in source_results],
    )
    full = config.model_dump(mode="json")
    original = repo.save_blob(
        project, io.BytesIO(json.dumps(full, ensure_ascii=False).encode()), "application/json"
    )
    payload = dict(
        contract=PERSISTED,
        version=VERSION,
        sectioning=SECTIONING,
        content=packet.model_dump(mode="json"),
        bindings=bindings,
        preparation=full,
        original_input_blob=original.model_dump(mode="json"),
    )
    ref = RuntimeRef(
        record_kind="user_input", record_id=uuid5(project, "research-editorial-input:" + digest(payload))
    )
    publish(repo, project, ref, payload)
    return ref
