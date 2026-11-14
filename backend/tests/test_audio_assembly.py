import json
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from podgen.audio.assembly.pcm import concatenate, read
from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.assembly.contracts import AssemblyConfig
from podgen.runtime.evaluation.audio.assembly.source import prepare_sources
from podgen.runtime.evaluation.audio.fidelity.contracts import load_fidelity_config
from tests.audio_cases import MemoryJournal, signal
from tests.test_audio_fidelity_evaluation import setup


def test_concatenation_is_bit_exact_with_ordered_complete_frame_map():
    first, second = signal(seconds=3), signal(seconds=2, extremes=True)
    master, proof = concatenate([first, second])
    params, frames, pcm = read(master)
    assert params == (1, 2, 24000) and frames == 120000
    assert pcm == read(first)[2] + read(second)[2]
    assert [(p["master_start_frame"], p["master_end_frame"]) for p in proof["parts"]] == [
        (0, 72000),
        (72000, 120000),
    ]
    assert [p["source_start_frame"] for p in proof["parts"]] == [0, 0]
    assert (
        proof["cuts"] is proof["crossfades"] is proof["inserted_silence"] is proof["tempo_changed"] is False
    )
    with pytest.raises(ContractError):
        concatenate([first, signal(rate=48000)])
    with pytest.raises(ContractError):
        concatenate([first, second[:-1]])


@pytest.mark.parametrize("problem", ["reverse", "gap", "profile", "style", "script"])
def test_source_coverage_and_identity_are_checked_before_assembly(monkeypatch, problem):
    journal = MemoryJournal()
    source = dict(
        source_script_ref=journal.ref("script").model_dump(mode="json"),
        source_script_hash="exact",
        outcome_ref=journal.ref("outcome").model_dump(mode="json"),
        source=dict(direction=dict(style=""), utterances=[]),
        selectors=[],
    )
    parts = []
    for i in range(2):
        item = deepcopy(source)
        item["source"]["utterances"] = [dict(utterance_id=str(uuid4()), speaker="HOST", text=f"Реплика {i}.")]
        item["selectors"] = [
            dict(
                utterance_id=item["source"]["utterances"][0]["utterance_id"],
                utterance_index=i,
                char_start=0,
                char_end=10,
            )
        ]
        parts.append(dict(profile=dict(voice_id="same"), source=item))
    full = deepcopy(source)
    full["source"]["utterances"] = [u for p in parts for u in p["source"]["source"]["utterances"]]
    full["selectors"] = [s for p in parts for s in p["source"]["selectors"]]
    journal.put("outcome", dict(output=dict(utterances=full["source"]["utterances"])))
    if problem == "reverse":
        parts.reverse()
    elif problem == "gap":
        parts = parts[:1]
    elif problem == "profile":
        parts[1]["profile"]["voice_id"] = "other"
    elif problem == "style":
        parts[1]["source"]["source"]["direction"]["style"] = "other"
    else:
        parts[1]["source"]["source_script_hash"] = "other"
    refs = [journal.ref(f"part{i}") for i in range(len(parts))]
    monkeypatch.setattr(
        "podgen.runtime.evaluation.audio.assembly.source.comparison_results",
        lambda j, rs: [parts[refs.index(r)] for r in rs],
    )
    monkeypatch.setattr("podgen.runtime.evaluation.audio.assembly.source.import_fragment", lambda *a: full)
    c = AssemblyConfig(assembly_id=uuid4(), comparison_run_id=journal.run_id, part_result_refs=refs)
    with pytest.raises(ContractError):
        prepare_sources(journal, c)


def test_fidelity_v1_serialization_and_hash_remain_unchanged_with_v2_subjects(monkeypatch):
    _, config = setup(monkeypatch)
    raw = config.model_dump(mode="json")
    loaded = load_fidelity_config(json.dumps(raw))
    assert canonical_bytes(loaded.model_dump(mode="json")) == canonical_bytes(raw)
    assert "subject" not in raw and loaded.subject_ref == loaded.result_ref
    second = dict(
        raw, contract="audio-fidelity-evaluation/2", subject=dict(kind="assembly", ref=raw["result_ref"])
    )
    second.pop("result_ref")
    value = load_fidelity_config(json.dumps(second))
    assert value.subject_kind == "assembly" and "result_ref" not in value.model_dump(mode="json")
    with pytest.raises(ValidationError):
        load_fidelity_config(json.dumps(dict(raw, subject=second["subject"])))
