"""Append-only source additions scoped to one writing run, with exact retained bytes."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.text_source import verified_text
from podgen.sources.material_requests import SourceAdmission

from .sources import freeze_sources


def own_entries(journal):
    index = 0
    while (row := journal.get(f"material-admission:{index}")) is not None:
        yield dict(row, _admission_ref=journal.ref(f"material-admission:{index}").model_dump(mode="json"))
        index += 1


def entries(journal):
    inherited = journal.get("material-inheritance") or {}
    for ref in inherited.get("admission_refs", []):
        row = journal.repo.get_runtime(journal.project, RuntimeRef.model_validate(ref))
        yield dict(row, _admission_ref=ref)
    yield from own_entries(journal)


def load_admission(journal, row):
    source = dict(
        row["source"],
        admission_ref=row.get("_admission_ref") or journal.ref(row["record_key"]).model_dump(mode="json"),
    )
    if source.get("kind") == "research_report":
        ref = RuntimeRef.model_validate(source["report_ref"])
        saved = journal.repo.get_runtime(journal.project, ref)
        blob = BlobRef.model_validate(saved["report_text_blob"])
        if blob.sha256 != row["sha256"]:
            raise ContractError("research_added_report_hash_mismatch")
        text = verified_text(journal.repo, journal.project, ref, blob)
        text.verify()
        return SourceAdmission(source["source_id"], source["title"], text, blob.sha256, ref, source)
    ref = ArtifactRef.model_validate(source["representation_ref"])
    representation = journal.repo.get(journal.project, ref).payload
    if representation["source_revision_ref"] != source["source_revision_ref"]:
        raise ContractError("research_added_source_revision_mismatch")
    blob = BlobRef.model_validate(representation["canonical_text_blob"] or representation["content_blob"])
    if blob.sha256 != row["sha256"]:
        raise ContractError("research_added_source_hash_mismatch")
    text = verified_text(journal.repo, journal.project, ref, blob)
    text.verify()
    return SourceAdmission(source["source_id"], source["title"], text, blob.sha256, ref, source)


def admit_outcome(journal, materials, url, outcome):
    prior = list(entries(journal))
    matching = next((row for row in prior if row["url"] == url), None)
    if matching:
        return load_admission(journal, matching)
    numbers = [int(source.source_id[1:]) for source in materials.packet.sources]
    numbers.extend(
        int(row["source"]["source_id"][1:]) for row in prior if row["source"]["source_id"].startswith("S")
    )
    number = max(numbers, default=0) + 1
    sources, documents, _ = freeze_sources(
        journal.repo,
        journal.project,
        dict(material=dict(reported_sources=[])),
        {number: dict(url=url, label=url)},
        [(None, dict(sources=[dict(reported_url=url, acquisition=outcome)]))],
        [],
    )
    source = sources[0]
    if not source.document:
        return None
    record_key = f"material-admission:{sum(1 for _ in own_entries(journal))}"
    catalog = source.model_dump(mode="json")
    journal.put(
        record_key,
        dict(
            url=url,
            record_key=record_key,
            source=catalog,
            sha256=documents[0].sha256,
            shared_input_ref=materials.shared_ref.model_dump(mode="json"),
        ),
    )
    return load_admission(journal, journal.get(record_key))


def admit_report(journal, materials, child, title, source_map):
    prior = list(entries(journal))
    ref = child.ref("result").model_dump(mode="json")
    existing = next((row for row in prior if row["source"].get("report_ref") == ref), None)
    if existing:
        return load_admission(journal, existing)
    index = 1 + max(
        (
            int(row["source"]["source_id"][1:])
            for row in prior
            if row["source"].get("kind") == "research_report"
        ),
        default=0,
    )
    record_key = f"material-admission:{sum(1 for _ in own_entries(journal))}"
    source = dict(
        source_id=f"X{index}",
        title=title,
        kind="research_report",
        report_ref=ref,
        reported_source_map=source_map,
    )
    journal.put(
        record_key,
        dict(
            url=None,
            record_key=record_key,
            source=source,
            sha256=child.get("result")["report_text_blob"]["sha256"],
            shared_input_ref=materials.shared_ref.model_dump(mode="json"),
        ),
    )
    return load_admission(journal, journal.get(record_key))
