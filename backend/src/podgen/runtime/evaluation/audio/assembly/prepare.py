from podgen.audio.media.processing import tools
from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError

from ..fidelity.snapshots import schema_blob
from .contracts import AssemblyConfig, prefix
from .source import prepare_sources


def prepare(journal, config):
    if journal.run_id != config.comparison_run_id:
        raise ContractError("assembly_wrong_comparison_run")
    key = prefix(config.assembly_id)
    raw = config.model_dump(mode="json")
    saved = journal.get(key + ":manifest")
    if saved:
        if saved["configuration_hash"] != digest(raw):
            raise Conflict("assembly_configuration_changed")
        return saved
    full, parts = prepare_sources(journal, config)
    source_ref = journal.put(key + ":source", full)
    manifest = dict(
        contract="audio-evaluation-assembly-manifest/1",
        configuration=raw,
        configuration_hash=digest(raw),
        source_ref=source_ref.model_dump(mode="json"),
        source_script_ref=full["source_script_ref"],
        source_script_hash=full["source_script_hash"],
        comparison_manifest_ref=journal.ref("manifest").model_dump(mode="json"),
        profile=parts[0]["profile"],
        direction=full["source"]["direction"],
        parts=[
            dict(
                result_ref=ref.model_dump(mode="json"),
                source_ref=p["source_ref"],
                decoded_ref=p["decoded_ref"],
                audio_blob=p["audio_blob"],
                selectors=p["source"]["selectors"],
            )
            for ref, p in zip(config.part_result_refs, parts, strict=True)
        ],
        config_schema_blob=schema_blob(journal, AssemblyConfig),
        tool_versions=tools(),
        coverage=dict(
            full_script=True,
            utterances=len(full["source"]["utterances"]),
            duplicates=[],
            gaps=[],
            basis="source_selections_and_complete_pcm_parts",
            spoken_word_fidelity="pending",
        ),
        production_ready=False,
    )
    journal.put(key + ":manifest", manifest)
    return manifest
