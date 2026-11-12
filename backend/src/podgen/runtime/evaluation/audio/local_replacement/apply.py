import hashlib
import json
from uuid import uuid5

from podgen.audio.assembly.pcm import read
from podgen.audio.editing.splice import render, splice
from podgen.audio.editing.timeline import replace
from podgen.audio.media.processing import decode, tools
from podgen.audio.synthesis.contracts import SynthesizedAudio
from podgen.domain.canonical import canonical_bytes, digest
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from ..subject_contracts import AudioSubject
from ..subjects import audio_subject
from .approval import authorized
from .attempts import state
from .contracts import contextual, prefix
from .localization import binding, current
from .previews import excerpts


def apply(journal, proposal_ids, *, parent_subject=None):
    if not proposal_ids or len(set(proposal_ids)) != len(proposal_ids):
        raise ContractError("local_replacement_apply_requires_unique_proposals")
    with journal.lock():
        proposals, takes = [], []
        for proposal_id in proposal_ids:
            proposal, _ = authorized(journal, proposal_id)
            rows = attempts(journal, prefix(proposal_id))
            if not rows or state(journal, rows[-1]["key"]) != "synthesized":
                raise ContractError("local_replacement_apply_requires_current_synthesized_take")
            last = rows[-1]["processing"][-1]
            if last["result"]["proposal_hash"] != proposal["proposal_hash"]:
                raise ContractError("local_replacement_take_for_different_proposal")
            proposals.append((proposal_id, proposal["payload"], proposal["proposal_hash"]))
            if contextual(proposal["payload"]):
                if current(journal, proposal_id) != "extraction_ready":
                    raise ContractError("local_replacement_apply_requires_extraction_ready")
                _, _, _, extraction_key = binding(journal, proposal_id)
                extracted = journal.get(extraction_key + ":result")
                if extracted["contextual_audio_sha256"] != last["result"]["decoded_pcm_blob"]["sha256"]:
                    raise ContractError("local_replacement_extraction_audio_mismatch")
                takes.append((journal.ref(extraction_key + ":result"), extracted))
            else:
                takes.append((journal.ref(last["key"] + ":result"), last["result"]))
        paired = sorted(zip(proposals, takes, strict=True), key=lambda pair: str(pair[0][0]))
        proposals = [pair[0] for pair in paired]
        takes = [pair[1] for pair in paired]
        original = proposals[0][1]
        if (
            parent_subject is not None
            and parent_subject.model_dump(mode="json") != original["parent_subject"]
        ):
            raise ContractError("local_replacement_stale_parent_requires_new_proposal_approval")
        if any(
            p[1]["parent_subject"] != original["parent_subject"]
            or p[1]["timeline_blob"] != original["timeline_blob"]
            for p in proposals
        ):
            raise ContractError("local_replacement_batch_requires_same_exact_parent_and_timeline")
        identity = sorted(
            (str(p[0]), p[2], str(t[0].record_id)) for p, t in zip(proposals, takes, strict=True)
        )
        version_id = uuid5(journal.run_id, "local-master:" + digest([list(row) for row in identity]))
        key = "local-master:" + str(version_id) + ":result"
        if journal.get(key):
            return journal.ref(key).model_dump(mode="json")
        parent = AudioSubject.model_validate(original["parent_subject"])
        subject = audio_subject(journal, parent)
        if subject["audio_blob"]["sha256"] != original["parent_audio_sha256"]:
            raise ContractError("local_replacement_parent_audio_changed")
        audio = journal.base.read_blob(parent.ref, BlobRef.model_validate(subject["audio_blob"]))
        if hashlib.sha256(audio).hexdigest() != original["parent_audio_sha256"]:
            raise ContractError("local_replacement_parent_audio_changed")
        params, _, _ = read(audio)
        timeline = json.loads(journal.bytes(prefix(proposals[0][0]) + ":proposal", original["timeline_blob"]))
        replacements = []
        for (proposal_id, payload, proposal_hash), (take_ref, take) in zip(proposals, takes, strict=True):
            if payload["tool_versions"] != tools():
                raise ContractError("local_replacement_render_tools_changed")
            decoded = journal.base.read_blob(
                take_ref,
                BlobRef.model_validate(
                    take["extracted_audio_blob"] if contextual(payload) else take["decoded_pcm_blob"]
                ),
            )
            rendered, transform = render(decoded, params, payload["parent_normalization"])
            rendered_key = f"local-master:{version_id}:insert:{proposal_id}"
            rendered_blob = journal.blob(rendered, "audio/wav")
            rendered_ref = journal.put(
                rendered_key,
                dict(
                    contract="audio-local-rendered-insert/1",
                    audio_blob=rendered_blob,
                    sample_rate_hz=params[2],
                    sample_width_bytes=params[1],
                    sample_frames=read(rendered)[1],
                    provenance_ref=take_ref.model_dump(mode="json"),
                    transform=transform,
                ),
            )
            replacements.append(
                dict(
                    payload["frame_range"],
                    audio=rendered,
                    proposal_id=str(proposal_id),
                    proposal_hash=proposal_hash,
                    take_ref=take_ref.model_dump(mode="json"),
                    transform=transform,
                    rendered_insert_ref=rendered_ref.model_dump(mode="json"),
                    context_seconds=payload["configuration"]["context_seconds"],
                    script_ranges=payload["script_ranges"],
                    approval_ref=journal.ref(prefix(proposal_id) + ":approval").model_dump(mode="json"),
                )
            )
        replacements.sort(key=lambda row: row["start_frame"])
        master, mapping, proof = splice(audio, replacements)
        edits, delta, joins, auditions = [], 0, [], []
        for row in replacements:
            inserted_frames = read(row["audio"])[1]
            start = row["start_frame"] + delta
            end = start + inserted_frames
            difference = inserted_frames - (row["end_frame"] - row["start_frame"])
            edit = dict((k, v) for k, v in row.items() if k != "audio")
            edit.update(
                new_start_frame=start,
                new_end_frame=end,
                delta_frames=difference,
                inserted_audio_blob=journal.blob(row["audio"], "audio/wav"),
            )
            edits.append(edit)
            delta += difference
            audition, edit_joins = excerpts(
                journal,
                master,
                params[2],
                proof["sample_frames"],
                start,
                end,
                row["context_seconds"],
                row["proposal_id"],
            )
            auditions.append(audition)
            joins.extend(edit_joins)
        for row in mapping:
            if row["kind"] == "parent_audio":
                row["source_audio_ref"] = parent.ref.model_dump(mode="json")
            else:
                edit = next(edit for edit in edits if edit["proposal_id"] == row["proposal_id"])
                row["source_audio_ref"] = edit["rendered_insert_ref"]
                row["source_audio_blob"] = edit["inserted_audio_blob"]
                row["provenance_ref"] = edit["take_ref"]
            row["sample_rate_hz"] = params[2]
            row["sample_width_bytes"] = params[1]
        updated = replace(timeline, edits)
        _, metrics = decode(SynthesizedAudio(master, "audio/wav", params[2], "wav", None, {}, "local_splice"))
        if metrics["sample_frames"] != proof["sample_frames"]:
            raise ContractError("local_replacement_full_decode_frame_mismatch")
        final_blob = journal.blob(master, "audio/wav")
        result = dict(
            contract="audio-local-master/1",
            version_id=str(version_id),
            comparison_run_id=str(journal.run_id),
            parent_subject=original["parent_subject"],
            parent_audio_sha256=original["parent_audio_sha256"],
            source_ref=original["source_ref"],
            source_script_ref=original["source_script_ref"],
            source_script_hash=original["source_script_hash"],
            profile=original["profile"],
            master_pcm_blob=final_blob,
            playback_audio_blob=final_blob,
            metrics=metrics,
            timeline_blob=journal.blob(canonical_bytes(updated), "application/json"),
            edits=edits,
            frame_map=mapping,
            splice_proof=proof,
            recognition_origin=original["recognition_origin"],
            normalization=dict(original["parent_normalization"], local_replacement_transforms_only=True),
            joins=joins,
            auditions=auditions,
            error_source="human",
            listening_status="not_reviewed",
            production_ready=False,
        )
        journal.put(key, result)
        return journal.ref(key).model_dump(mode="json")
