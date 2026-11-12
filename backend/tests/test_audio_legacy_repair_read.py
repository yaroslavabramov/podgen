from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.assembly.legacy_repair import resolve
from podgen.runtime.evaluation.audio.subject_contracts import SynthesisPart
from podgen.runtime.evaluation.audio.subjects import synthesis_part
from tests.audio_local_replacement_fixtures import fixture


def test_saved_whole_fragment_part_remains_readable_without_old_generator(monkeypatch):
    journal, config, _ = fixture(monkeypatch)
    repair_id = uuid4()
    root = "fragment-repair:" + str(repair_id)
    source_ref = journal.ref("source").model_dump(mode="json")
    audio = journal.base.read(config.parent_subject.ref)["master_pcm_blob"]
    decoded_ref = journal.put(
        "legacy-decoded", dict(pcm_blob=audio, metrics=dict(full_decode=True))
    ).model_dump(mode="json")
    journal.put(
        root + ":manifest",
        dict(
            configuration=dict(comparison_run_id=str(journal.run_id)),
            source_ref=source_ref,
            profile=dict(frozen=True),
            original_request=dict(frozen=True),
            wire_blob=audio,
            origin_part_ref=config.parent_subject.ref.model_dump(mode="json"),
        ),
    )
    attempt = root + ":attempt:1"
    journal.put(attempt + ":prepared", dict(frozen=True))
    processing = attempt + ":processing:1"
    journal.put(processing, dict(frozen=True))
    result = dict(
        contract="audio-evaluation-repair-part/1",
        repair_id=str(repair_id),
        manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"),
        source_ref=source_ref,
        decoded_ref=decoded_ref,
        decoded_pcm_blob=audio,
        repair_depth=1,
    )
    ref = journal.put(processing + ":result", result)
    found = synthesis_part(journal, SynthesisPart(kind="repair_part", ref=ref))
    assert found["audio_blob"] == audio and found["result"] == result
    assert found["original_request"] == dict(frozen=True)
    with pytest.raises(ContractError, match="outside_comparison"):
        resolve(journal, journal.record(result))
