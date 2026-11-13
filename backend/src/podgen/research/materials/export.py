"""Export the exact admitted report, retained source texts and provenance without rewriting."""

import re
from itertools import islice

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import generation_repo

from .loading import load_materials


def export_materials(repo, project, manifest, ref, shared, *, write_json, write_bytes):
    from podgen.writing.research_materials.pinning import validate

    generation = generation_repo(repo)
    materials = load_materials(generation, project, ref, case_id=manifest["command"]["case_id"])
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
    from podgen.persistence.read_authorization import read_operation
    from podgen.persistence.source_reading import read_sources

    with read_operation(repo):
        write_json("research/report-result.json", repo.get_runtime(project, packet.report_result_ref))
    references = iter(enumerate(packet.source_result_refs, 1))
    while group := list(islice(references, 64)):
        with read_operation(repo):
            records = repo.get_runtime_many(project, [ref for _, ref in group])
            for (index, _), record in zip(group, records, strict=True):
                write_json(f"research/acquisitions/{index}.json", record)
    sources = iter(source for source in packet.sources if source.document is not None)
    while group := list(islice(sources, 64)):
        with read_operation(generation), read_sources([documents[source.document] for source in group]):
            for source in group:
                # A previous source's remote object read may have reset this
                # operation. Plain-text historical inputs need this check too.
                materials.authorize()
                if not re.fullmatch(r"S[1-9]\d*", source.source_id):
                    raise ContractError("research_invalid_source_export_identifier")
                write_bytes(
                    f"research/sources/{source.source_id}.txt",
                    documents[source.document].text.encode("utf-8"),
                )
            materials.authorize()
    return packet
