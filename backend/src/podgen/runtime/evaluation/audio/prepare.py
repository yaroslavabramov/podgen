from uuid import uuid5

from podgen.audio.media.processing import tools
from podgen.audio.synthesis.contracts import SynthesisInput
from podgen.audio.synthesis.registry import adapter
from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, Conflict

from .source import import_fragment


def prepare(journal, config):
    raw = config.model_dump(mode="json")
    saved = journal.get("manifest")
    if saved:
        if saved["actor_id"] != str(journal.repo.actor_id):
            raise AccessDenied()
        if saved["configuration_hash"] != digest(raw):
            raise Conflict("audio_run_configuration_changed")
        return saved
    versions = tools()
    inputs, cache = {}, {}
    for fragment in config.fragments:
        value = import_fragment(journal, fragment, cache)
        ref = journal.put("source:" + fragment.fragment_id, value)
        inputs[fragment.fragment_id] = dict(ref=ref.model_dump(mode="json"), value=value)
    cells = []
    for profile in config.profiles:
        port = adapter(profile.synthesis.provider)
        for fragment in config.fragments:
            source = inputs[fragment.fragment_id]
            request = port.prepare(
                profile.synthesis, SynthesisInput.model_validate(source["value"]["source"])
            )
            cell_id = str(uuid5(config.run_id, f"audio-sample:{profile.profile_id}:{fragment.fragment_id}"))
            cells.append(
                dict(
                    cell_id=cell_id,
                    profile_id=profile.profile_id,
                    fragment_id=fragment.fragment_id,
                    source_ref=source["ref"],
                    request=request.model_dump(mode="json"),
                )
            )
    availability = [
        dict(
            provider=provider,
            configured_profiles=sum(p.synthesis.provider == provider for p in config.profiles),
            expected_profiles=config.voice_counts[provider],
            status="profiles_configured"
            if sum(p.synthesis.provider == provider for p in config.profiles) == config.voice_counts[provider]
            else "not_configured",
        )
        for provider in config.providers
    ]
    manifest = dict(
        contract="audio-comparison-manifest/1",
        actor_id=str(journal.repo.actor_id),
        tool_versions=versions,
        purpose="evaluation",
        production_ready=False,
        semantic_acceptance=False,
        configuration=raw,
        configuration_hash=digest(raw),
        cells=cells,
        availability=availability,
        expected_samples=config.expected_samples,
        source_refs={key: value["ref"] for key, value in inputs.items()},
        processing_contract="ffmpeg-lossless-comparison/1",
    )
    journal.put("manifest", manifest)
    return manifest
