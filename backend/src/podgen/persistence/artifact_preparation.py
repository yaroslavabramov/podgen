"""Operation-owned byte verification, never an authorization grant or process cache."""

import hashlib
from dataclasses import dataclass, field

from podgen.domain.contracts import validate_payload
from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.errors import ContractError
from podgen.domain.references import walk_values

from .access_facts import access_facts
from .read_authorization import authorized_read, object_io


@dataclass(frozen=True)
class VerifiedBlob:
    owner: object
    project: object
    ref: BlobRef


def verified_bytes(repo, project, ref, body):
    if len(body) != ref.byte_length or hashlib.sha256(body).hexdigest() != ref.sha256:
        raise ContractError("blob_integrity_error")
    return VerifiedBlob(repo.objects, project, ref)


@dataclass
class PreparedPublication:
    owner: object
    project: object
    blobs: set = field(default_factory=set)
    texts: dict = field(default_factory=dict)

    def require(self, repo, project, ref):
        if self.owner is not repo.objects or self.project != project or ref not in self.blobs:
            raise ContractError("artifact_bytes_not_prepared")


def prepare_blobs(repo, project, references, *, public_only, verified=(), text_refs=()):
    value = PreparedPublication(repo.objects, project)
    for proof in verified:
        if proof.owner is not repo.objects or proof.project != project:
            raise ContractError("artifact_bytes_scope_mismatch")
        value.blobs.add(proof.ref)
    text_refs = set(text_refs)
    missing = (set(references) - value.blobs) | text_refs
    if missing:
        with repo.database.read_transaction() as tx, authorized_read(repo, tx):
            repo._acl(tx, project, write=True)
            facts = access_facts(repo, tx)
            facts.blobs.load(project, [ref.storage_key for ref in missing])
            for ref in missing:
                facts.blobs.check(project, ref, public_only=public_only)
        with object_io(repo):
            for ref in missing:
                checksum, length = hashlib.sha256(), 0
                text = bytearray() if ref in text_refs else None
                chunks = (
                    repo.objects.iter_read(project, ref)
                    if hasattr(repo.objects, "iter_read")
                    else (repo.objects.read(project, ref),)
                )
                for chunk in chunks:
                    checksum.update(chunk)
                    length += len(chunk)
                    if text is not None:
                        text.extend(chunk)
                if length != ref.byte_length or checksum.hexdigest() != ref.sha256:
                    raise ContractError("blob_integrity_error")
                value.blobs.add(ref)
                if text is not None:
                    value.texts[ref] = text.decode("utf-8")
    return value


def prepare(repo, project, kind, payload, *, audience="project", verified=()):
    from .repository import HIDDEN_KINDS

    validate_payload(kind, payload)
    if audience not in ("project", "evaluator"):
        raise ContractError("invalid_audience")
    required = {ref for _, _, ref in walk_values(payload) if isinstance(ref, BlobRef)}
    text_refs = []
    if kind == "EvidenceSpan" and payload["selector"]["kind"] == "text":
        representation = repo.get(project, ArtifactRef.model_validate(payload["representation_ref"]))
        blob = BlobRef.model_validate(
            representation.payload.get("canonical_text_blob") or representation.payload["content_blob"]
        )
        text_refs.append(blob)
    return prepare_blobs(
        repo,
        project,
        required,
        public_only=audience == "project" and kind not in HIDDEN_KINDS,
        verified=verified,
        text_refs=text_refs,
    )


def prepare_package(repo, project, package, refs, runtimes, blobs, root):
    """An atomic package may create the representation before its evidence.

    Discover only the canonical texts actually needed by text evidence; do not
    decode unrelated image/audio blobs or enter object storage from its SQL phase.
    """
    from podgen.domain.packages import substitute

    required = set(blobs.values())
    for item in package.artifacts:
        required.update(ref for _, _, ref in walk_values(item.payload) if isinstance(ref, BlobRef))
    texts = set()
    nodes = {item.key: item for item in package.artifacts}
    for item in package.artifacts:
        if item.kind != "EvidenceSpan" or item.payload["selector"]["kind"] != "text":
            continue
        target = item.payload["representation_ref"]
        key = target.get("$artifact")
        if key in nodes:
            body = nodes[key].payload
        else:
            reference = refs[key] if key is not None else ArtifactRef.model_validate(target)
            body = repo.get(project, reference).payload
        blob = BlobRef.model_validate(
            substitute(body.get("canonical_text_blob") or body["content_blob"], refs, runtimes, blobs, root)
        )
        texts.add(blob)
    return prepare_blobs(repo, project, required, public_only=False, text_refs=texts)
