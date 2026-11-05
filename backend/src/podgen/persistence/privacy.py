"""Privacy follows provenance AND aliases of identical bytes. Quality never restores access."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.references import walk_values

from .database import query


def protected_hashes(tx, project_id, *, artifact_ids=(), runtime_ids=()):
    artifacts, runtimes, hashes = set(artifact_ids), set(runtime_ids), set()
    links = list(
        query(
            tx, "SELECT consumer_id,input_id FROM revision_links WHERE project_id=:p", p=project_id
        ).mappings()
    )
    links += list(
        query(
            tx,
            "SELECT subject_id AS consumer_id,input_id FROM revalidation_privacy_links WHERE project_id=:p",
            p=project_id,
        ).mappings()
    )
    runtime_refs = {
        r["record_id"]: [ref for _, _, ref in walk_values(r["payload"])]
        for r in query(
            tx, "SELECT record_id,payload FROM runtime_records WHERE project_id=:p", p=project_id
        ).mappings()
    }
    runtime_links = list(
        query(
            tx, "SELECT revision_id,record_id FROM runtime_links WHERE project_id=:p", p=project_id
        ).mappings()
    )
    blob_links = [
        (r["revision_id"], r["sha256"])
        for r in query(
            tx,
            "SELECT a.revision_id,b.sha256 FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key) WHERE a.project_id=:p",
            p=project_id,
        ).mappings()
    ]
    blob_links += [
        (r["revision_id"], asset["blob_ref"]["sha256"])
        for r in query(
            tx, "SELECT revision_id,config FROM catalog_publications WHERE project_id=:p", p=project_id
        ).mappings()
        for asset in r["config"]["published_assets"]
    ]
    changed = True
    while changed:
        before = (len(artifacts), len(runtimes), len(hashes))
        for link in links:
            if link["input_id"] in artifacts:
                artifacts.add(link["consumer_id"])
        for identity, refs in runtime_refs.items():
            if any(
                isinstance(r, ArtifactRef)
                and r.revision_id in artifacts
                or isinstance(r, RuntimeRef)
                and r.record_id in runtimes
                or isinstance(r, BlobRef)
                and r.sha256 in hashes
                for r in refs
            ):
                runtimes.add(identity)
            if identity in runtimes:
                hashes.update(r.sha256 for r in refs if isinstance(r, BlobRef))
        for link in runtime_links:
            if link["record_id"] in runtimes:
                artifacts.add(link["revision_id"])
        for identity, checksum in blob_links:
            if identity in artifacts:
                hashes.add(checksum)
            if checksum in hashes:
                artifacts.add(identity)
        changed = before != (len(artifacts), len(runtimes), len(hashes))
    return hashes


def revoke_content(tx, project_id, revision_id, event_id, reason):
    for checksum in protected_hashes(tx, project_id, artifact_ids=[revision_id]):
        query(
            tx,
            "INSERT INTO blob_access_revocations VALUES(:p,:sha,:id,:reason) ON CONFLICT DO NOTHING",
            p=project_id,
            sha=checksum,
            id=event_id,
            reason=reason,
        )
