import json
from uuid import UUID

from podgen.audio.assembly.pcm import read
from podgen.audio.editing.timeline import build
from podgen.audio.recognition.contracts import Recognition
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError

from ..fidelity.contracts import prefix as audit_prefix
from ..fidelity.processing import revisions
from ..subjects import audio_subject


def resolve(journal, ref):
    result = journal.base.read(ref)
    if result.get("contract") != "audio-local-master/1" or result["comparison_run_id"] != str(journal.run_id):
        raise ContractError("local_master_outside_comparison")
    key = "local-master:" + str(UUID(result["version_id"])) + ":result"
    if journal.ref(key) != ref or journal.get(key) != result:
        raise ContractError("local_master_not_registered")
    source = journal.base.read(RuntimeRef.model_validate(result["source_ref"]))
    return dict(
        kind="local_master",
        audio_ref=ref.model_dump(mode="json"),
        result=result,
        audio_blob=result["master_pcm_blob"],
        playback_audio_blob=result["master_pcm_blob"],
        audio_variant="human_replaced_final_playback_pcm",
        decoded_ref=None,
        playback_normalization=result["normalization"],
        source_ref=result["source_ref"],
        source=source,
        profile=result["profile"],
    )


def load(journal, config):
    parent = audio_subject(journal, config.parent_subject)
    audio = journal.base.read_blob(config.parent_subject.ref, BlobRef.model_validate(parent["audio_blob"]))
    params, frames, pcm = read(audio)
    if config.approximate_seconds * params[2] >= frames:
        raise ContractError("local_replacement_time_outside_master")
    if config.recognition_audit_id is None:
        if parent["kind"] != "local_master":
            raise ContractError("local_replacement_requires_saved_master_recognition")
        timeline = json.loads(
            journal.base.read_blob(
                config.parent_subject.ref, BlobRef.model_validate(parent["result"]["timeline_blob"])
            )
        )
        return parent, audio, params, frames, timeline, parent["result"].get("recognition_origin")
    root = audit_prefix(config.recognition_audit_id)
    audit = journal.get(root + ":manifest")
    if (
        not audit
        or audit["audio_blob"]["sha256"] != parent["audio_blob"]["sha256"]
        or audit.get("subject_ref") != config.parent_subject.ref.model_dump(mode="json")
    ):
        raise ContractError("local_replacement_recognition_is_for_different_master")
    saved = next(
        (row for row in reversed(revisions(journal, config.recognition_audit_id)) if row["result"]), None
    )
    if not saved:
        raise ContractError("local_replacement_recognition_not_available")
    recognized = journal.get(saved["key"] + ":recognition")
    if recognized["submitted_audio_sha256"] != parent["audio_blob"]["sha256"]:
        raise ContractError("local_replacement_recognition_audio_hash_mismatch")
    timeline = build(
        parent["source"]["source"]["utterances"],
        Recognition.model_validate(recognized["output"]),
        pcm,
        params[1],
        params[2],
        frames,
    )
    origin = dict(
        audit_id=str(config.recognition_audit_id),
        recognition_ref=journal.ref(saved["key"] + ":recognition").model_dump(mode="json"),
    )
    return parent, audio, params, frames, timeline, origin
