"""Export the exact admitted report, retained source texts and provenance without rewriting."""

import re

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import generation_repo

from .loading import load_materials


def export_materials(repo, project, manifest, ref, shared, *, write_json, write_bytes):
    from podgen.writing.research_materials.pinning import validate

    materials = load_materials(generation_repo(repo), project, ref, case_id=manifest["command"]["case_id"])
    validate(materials, manifest)
    packet = materials.packet
    documents = {spec.key: doc for spec, doc in zip(packet.documents, materials.documents, strict=True)}
    write_json("research/admission.json", shared)
    write_bytes(
        "research/preparation-original.json",
        repo.read_runtime_blob(project, ref, BlobRef.model_validate(shared["original_input_blob"])),
    )
    write_bytes("research/report.md", documents["report"].text.encode("utf-8"))
    write_json("research/sections.json", [row.model_dump(mode="json") for row in packet.sections])
    write_json("research/sources.json", [row.model_dump(mode="json") for row in packet.sources])
    write_json("research/report-result.json", repo.get_runtime(project, packet.report_result_ref))
    for index, result_ref in enumerate(packet.source_result_refs, 1):
        write_json(f"research/acquisitions/{index}.json", repo.get_runtime(project, result_ref))
    for source in packet.sources:
        if source.document is None:
            continue
        if not re.fullmatch(r"S[1-9]\d*", source.source_id):
            raise ContractError("research_invalid_source_export_identifier")
        write_bytes(
            f"research/sources/{source.source_id}.txt", documents[source.document].text.encode("utf-8")
        )
    return packet
