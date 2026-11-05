"""Cross-artifact bindings and exact selectors; semantic review remains a separate owner."""

import hashlib

from podgen.domain.contracts.common import ArtifactRef, TextSelector, select_text
from podgen.domain.contracts.writing import ScriptRevision, Utterance
from podgen.domain.errors import ContractError
from podgen.domain.spoken import script_selection, spoken_view


def validate_bindings(repo, tx, project_id, kind, payload, refs):
    def resolved(value):
        ref = ArtifactRef.model_validate(value)
        return repo._resolve(tx, project_id, ref)["payload"]

    def equal(actual, expected, code):
        if actual != expected:
            raise ContractError(code)

    def visit(value):
        if isinstance(value, dict):
            if set(value) == {"script_ref", "selector"}:
                script = ScriptRevision.model_validate(resolved(value["script_ref"]))
                from podgen.domain.contracts.common import LocalScriptSelector

                script_selection(script, LocalScriptSelector.model_validate(value["selector"]))
            if set(value) == {"note_ref", "field", "char_start", "char_end"}:
                text = resolved(value["note_ref"])["answer_markdown"]
                if value["field"] != "answer_markdown" or not 0 <= value["char_start"] <= value[
                    "char_end"
                ] <= len(text):
                    raise ContractError("note_selector_out_of_bounds")
            if set(value) == {"audio_ref", "start_frame", "end_frame"}:
                audio = resolved(value["audio_ref"])
                if (
                    value["audio_ref"]["kind"] not in ("AudioSegment", "AudioMaster")
                    or not 0 <= value["start_frame"] < value["end_frame"] <= audio["sample_frames"]
                ):
                    raise ContractError("audio_selector_out_of_bounds")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if kind == "CapabilityReport":
        profile_ref = ArtifactRef.model_validate(payload["profile_ref"])
        profile = repo._resolve(tx, project_id, profile_ref)
        if profile_ref.kind not in (
            "ModelProfile",
            "ProcessingProfile",
            "VoiceProfile",
            "PronunciationLexicon",
        ):
            raise ContractError("capability_profile_kind_mismatch")
        equal(
            payload["tested_configuration_fingerprint"],
            profile["content_hash"],
            "capability_fingerprint_mismatch",
        )
    if kind == "SourceRepresentation":
        profile_ref = ArtifactRef.model_validate(payload["extractor"]["profile_ref"])
        profile = resolved(payload["extractor"]["profile_ref"])
        if profile_ref.kind != "ProcessingProfile" or profile["profile_kind"] != "extraction":
            raise ContractError("extractor_profile_subtype_mismatch")
        from podgen.domain.canonical import digest

        equal(payload["extractor"]["name"], profile["implementation"], "extractor_implementation_mismatch")
        equal(payload["extractor"]["version"], profile["version"], "extractor_version_mismatch")
        equal(
            payload["extractor"]["configuration_hash"],
            digest(profile["configuration"]),
            "extractor_configuration_mismatch",
        )
    if kind == "PolicyBundle":
        for field in payload:
            equal(
                resolved(payload[field])["policy_kind"],
                field.removesuffix("_policy_ref"),
                "policy_bundle_subtype_mismatch",
            )
    if kind == "EpisodeSpec":
        bundle = resolved(payload["policy_ref"])
        equal(payload["source_policy_ref"], bundle["source_policy_ref"], "episode_source_policy_mismatch")
    if kind == "DossierSnapshot":
        spec = resolved(payload["episode_spec_ref"])
        equal(payload["knowledge_as_of"], spec["temporal"]["knowledge_as_of"], "dossier_cutoff_mismatch")
        equal(payload["source_policy_ref"], spec["source_policy_ref"], "dossier_source_policy_mismatch")
        if payload["overview_note_ref"] not in payload["note_refs"]:
            raise ContractError("overview_not_in_selected_notes")
        if payload["timeline_note_ref"] and payload["timeline_note_ref"] not in payload["note_refs"]:
            raise ContractError("timeline_not_in_selected_notes")
        # Exact source/evidence closure of the currently selected notes (not their superseded history).
        evidence_refs = {}
        source_refs = {}
        visited = set()

        def closure(ref_value):
            ref = ArtifactRef.model_validate(ref_value)
            if ref.revision_id in visited:
                return
            visited.add(ref.revision_id)
            body = resolved(ref_value)
            if ref.kind == "EvidenceSpan":
                evidence_refs[ref.revision_id] = ref_value
                source = body["source_revision_ref"]
                source_refs[source["revision_id"]] = source
                return
            if ref.kind == "ResearchNote":
                for citation in body["citations"]:
                    for evidence in citation["evidence_refs"]:
                        closure(evidence)

        for note in payload["note_refs"]:
            closure(note)
        equal(
            {r["revision_id"] for r in payload["evidence_refs"]},
            {str(k) for k in evidence_refs},
            "dossier_evidence_closure_mismatch",
        )
        equal(
            {r["revision_id"] for r in payload["source_revision_refs"]},
            set(source_refs),
            "dossier_source_closure_mismatch",
        )
    if kind == "StoryPlan":
        dossier = resolved(payload["dossier_ref"])
        equal(payload["episode_spec_ref"], dossier["episode_spec_ref"], "plan_spec_mismatch")
    if kind == "ScriptRevision":
        spec = resolved(payload["episode_spec_ref"])
        if payload["dossier_ref"]:
            equal(
                payload["episode_spec_ref"],
                resolved(payload["dossier_ref"])["episode_spec_ref"],
                "script_dossier_spec_mismatch",
            )
        if payload["story_plan_ref"]:
            plan = resolved(payload["story_plan_ref"])
            equal(payload["episode_spec_ref"], plan["episode_spec_ref"], "script_plan_spec_mismatch")
            equal(payload["dossier_ref"], plan["dossier_ref"], "script_plan_dossier_mismatch")
        script = ScriptRevision.model_validate(payload)
        speakers = {s["speaker_id"] for s in spec["speakers"]}
        spoken = {
            str(i.speaker_id)
            for a in script.acts
            for b in a.blocks
            for i in b.items
            if isinstance(i, Utterance)
        }
        if spoken - speakers:
            raise ContractError("unknown_script_speaker")
        if script.mode == "production" and script.completion == "complete" and spoken != speakers:
            raise ContractError("complete_script_missing_speaker")
        for binding in script.author_support_bindings:
            script_selection(script, binding.selector)
    if kind == "ScriptChangeSet":
        base, next_ = payload["base_script_ref"], payload["next_script_ref"]
        equal(resolved(next_)["parent_script_ref"], base, "changeset_parent_mismatch")
    if kind == "ResearchNote":
        for interpretation in payload["interpretations"]:
            selector = interpretation["text_selector"]
            if not 0 <= selector["char_start"] <= selector["char_end"] <= len(payload["answer_markdown"]):
                raise ContractError("note_selector_out_of_bounds")
            if set(interpretation["basis_markers"]) - {c["marker"] for c in payload["citations"]}:
                raise ContractError("unknown_citation_marker")
    if kind == "EvidenceSpan":
        representation = resolved(payload["representation_ref"])
        equal(
            payload["source_revision_ref"], representation["source_revision_ref"], "evidence_source_mismatch"
        )
        selector = payload["selector"]
        if selector["kind"] == "text":
            blob_value = representation.get("canonical_text_blob") or representation["content_blob"]
            from podgen.domain.contracts.common import BlobRef

            text = repo.objects.read(project_id, BlobRef.model_validate(blob_value)).decode("utf-8")
            selected = select_text(text, TextSelector.model_validate(selector))
            context = payload["context_selector"]
            if context["kind"] != "text":
                raise ContractError("text_evidence_requires_text_context")
            select_text(text, TextSelector.model_validate(context))
            if (
                not context["char_start"]
                <= selector["char_start"]
                <= selector["char_end"]
                <= context["char_end"]
            ):
                raise ContractError("context_does_not_cover_evidence")
            if set(selector["block_ids"]) - {b["block_id"] for b in representation["blocks"]}:
                raise ContractError("unknown_representation_block")
            if "exact_text" in payload and payload["exact_text"] != selected:
                raise ContractError("evidence_exact_text_mismatch")
            if (
                "exact_text_hash" in payload
                and payload["exact_text_hash"] != hashlib.sha256(selected.encode()).hexdigest()
            ):
                raise ContractError("evidence_exact_hash_mismatch")
    if kind == "ListenerState":
        ref = ArtifactRef.model_validate(payload["script_ref"])
        script = ScriptRevision.model_validate(resolved(payload["script_ref"]))
        prefix = payload["covered_prefix"]
        view = spoken_view(ref, script, len(prefix["ordered_item_ids"]))
        equal(
            prefix["ordered_item_ids"],
            [u["selector"]["item_id"] for u in view["utterances"]],
            "listener_prefix_not_contiguous",
        )
        equal(prefix["content_and_order_hash"], view["view_hash"], "listener_prefix_hash_mismatch")
    if kind in ("Transcript", "AudioQAReport"):
        expected = (
            ("audio_master_ref", "audio_segment_ref")
            if payload["scope"] == "master"
            else ("audio_segment_ref", "audio_master_ref")
        )
        if payload[expected[0]] is None or payload[expected[1]] is not None:
            raise ContractError("audio_scope_ref_mismatch")
    if (
        kind in ("VerificationReport", "EditorialReview", "ShowNotesReview")
        and payload["recommendation"] == "accepted"
    ):

        def blocking(value):
            if isinstance(value, dict):
                return value.get("blocking") is True or any(blocking(v) for v in value.values())
            return isinstance(value, list) and any(blocking(v) for v in value)

        if blocking(payload):
            raise ContractError("accepted_report_has_blocking_findings")
