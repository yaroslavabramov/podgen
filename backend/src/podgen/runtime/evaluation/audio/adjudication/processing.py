from podgen.audio.fidelity.adjudication.contracts import WindowResolution
from podgen.domain.errors import ContractError

from ..fidelity.snapshots import schema_blob
from .providers import adapter, cost_for


def revisions(journal, key):
    values = []
    number = 1
    while journal.get(f"{key}:processing:{number}") is not None:
        where = f"{key}:processing:{number}"
        values.append(
            dict(
                key=where,
                prepared=journal.get(where),
                result=journal.get(where + ":result"),
                failure=journal.get(where + ":failure"),
            )
        )
        number += 1
    return values


def process(journal, key, config, reason=None):
    provider = journal.get(key + ":prepared").get("provider", "gemini")
    port = adapter(provider, config)
    previous = revisions(journal, key)
    output = f"{key}:processing:{len(previous) + 1}"
    journal.put(
        output,
        dict(
            contract="audio-review-processing/1",
            adapter_version=port.VERSION,
            reason=reason,
            receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            response_schema_blob=schema_blob(journal, WindowResolution),
            parent_processing_ref=journal.ref(previous[-1]["key"]).model_dump(mode="json")
            if previous
            else None,
        ),
    )
    window_key = journal.get(key + ":prepared")["window_key"]
    window = journal.get(window_key)
    receipt = journal.get(key + ":receipt")
    try:
        extraction = window["extraction"]
        parsed, usage, request_id = port.parse(
            journal.bytes(key + ":receipt", receipt["raw_response_blob"]),
            window["case_ids"],
            extraction["sample_frames"] / extraction["sample_rate_hz"],
        )
        cost = cost_for(config, provider, usage)
        journal.put(
            output + ":result",
            dict(
                contract="audio-review-window-result/1",
                output=parsed.model_dump(mode="json"),
                decisions=[
                    dict(r.model_dump(mode="json"), candidate_id=window["case_aliases"][r.case_id])
                    for r in parsed.results
                ],
                request_id=request_id,
                usage=usage,
                cost=cost,
                window_ref=journal.ref(window_key).model_dump(mode="json"),
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
                production_ready=False,
                **(dict(provider=provider) if "provider" in journal.get(key + ":prepared") else {}),
            ),
        )
        return "reviewed"
    except (ValueError, KeyError, TypeError, ContractError) as error:
        journal.put(
            output + ":failure",
            dict(
                contract="audio-review-processing-failure/1",
                status="response_invalid",
                reason=str(error),
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        return "response_invalid"
