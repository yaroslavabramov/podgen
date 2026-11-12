"""Qualification-only TonePerfect wire adapter; never an acoustic pass/fail gate."""

import hashlib
import json

from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

VERSION = "toneperfect-qualification-adapter/1"
URL = "https://api.toneperfect.app/v1/assess"


def prepare(audio, text, idempotency_key):
    fields = dict(text=text, language="ru-RU", task="general")
    boundary = "podgen-toneperfect-" + hashlib.sha256(audio + canonical_bytes(fields)).hexdigest()
    if boundary.encode() in audio or any(boundary in v for v in fields.values()):
        raise ContractError("toneperfect_multipart_collision")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    wire = b"".join(parts)
    return dict(
        adapter_version=VERSION,
        method="POST",
        url=URL,
        fields=fields,
        headers={
            "content-type": "multipart/form-data; boundary=" + boundary,
            "accept": "application/json",
            "accept-encoding": "identity",
            "Idempotency-Key": idempotency_key,
            "TonePerfect-Fallback": "off",
            "TonePerfect-Debug": "raw",
        },
        wire_sha256=hashlib.sha256(wire).hexdigest(),
    ), wire


def parse(raw, headers, reservation):
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get("assessable")) is not bool:
        raise ContractError("toneperfect_invalid_assessment_response")
    billing = value.get("billing")
    if not isinstance(billing, dict) or type(billing.get("credits")) is not int or billing["credits"] < 0:
        raise ContractError("toneperfect_missing_credit_metadata")
    if value["assessable"] and (
        not isinstance(value.get("words"), list) or not isinstance(value.get("scores"), dict)
    ):
        raise ContractError("toneperfect_invalid_assessment_response")
    violations = []
    if value.get("test") is True or headers.get("x-toneperfect-test", "").lower() == "true":
        violations.append("mock_response")
    if billing.get("source") not in (None, "free") or (
        billing["credits"] > 0 and billing.get("source") != "free"
    ):
        violations.append("non_free_credit_source")
    if billing["credits"] > reservation:
        violations.append("charge_exceeds_reserved_credits")
    route = value.get("engine_route")
    if (isinstance(route, dict) and (route.get("engine") == "fallback" or route.get("fallback") is True)) or (
        value["assessable"] and (not isinstance(route, dict) or route.get("engine") != "primary")
    ):
        violations.append("primary_engine_not_confirmed")
    if value["assessable"] and (
        not isinstance(value.get("model_version"), str) or not value["model_version"]
    ):
        violations.append("model_version_missing")
    return dict(
        provider_response=value,
        policy_violations=violations,
        qualification_status="not_evaluated",
        production_ready=False,
    )
