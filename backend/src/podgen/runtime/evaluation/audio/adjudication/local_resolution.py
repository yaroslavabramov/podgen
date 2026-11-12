"""Only exact digital zero is resolved locally; this is never an acoustic verdict."""

from podgen.audio.fidelity.adjudication.contracts import WindowResolution


def key(window_key):
    return window_key + ":local-resolution"


def resolve_zero(journal, window_key, window):
    proof = window["providers"]["openai"]["audio_projection"]
    if not proof["source_all_zero"]:
        return
    output = WindowResolution.model_validate(
        dict(
            results=[
                dict(
                    case_id=case_id,
                    status="inconclusive",
                    reason_code="audio_unavailable",
                    heard_spans=[],
                    explanation="Все исходные PCM samples равны нулю; доступного речевого сигнала нет. API не вызывался.",
                )
                for case_id in window["case_ids"]
            ]
        )
    )
    journal.put(
        key(window_key),
        dict(
            contract="audio-review-local-zero-result/1",
            provider="local_pcm_preflight",
            basis="all_source_pcm_samples_exactly_zero",
            audio_projection=proof,
            output=output.model_dump(mode="json"),
            decisions=[
                dict(decision.model_dump(mode="json"), candidate_id=window["case_aliases"][decision.case_id])
                for decision in output.results
            ],
            cost=dict(status="no_api_request", usd="0"),
            window_ref=journal.ref(window_key).model_dump(mode="json"),
            production_ready=False,
        ),
    )
