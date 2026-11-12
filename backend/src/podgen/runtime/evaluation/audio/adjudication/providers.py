"""Provider selection and conservative modality-aware accounting."""

from decimal import Decimal

from podgen.audio.fidelity.adjudication import gemini, openai, realtime

from .contracts import ReviewConfigV3


def settings(config, provider):
    return config.fallback if provider == "openai" else config


def adapter(provider, config=None):
    if provider == "openai" and isinstance(config, ReviewConfigV3):
        return realtime
    return openai if provider == "openai" else gemini


def request_for(window, provider):
    return window["providers"][provider] if "providers" in window else window


def cost_for(config, provider, usage):
    profile = settings(config, provider)
    fallback = dict(status="reservation_not_invoice", usd=profile.reservation_per_request_usd)
    if provider == "openai":
        if isinstance(config, ReviewConfigV3):
            return realtime_cost(profile, usage, fallback)
        details = usage.get("prompt_tokens_details")
        output = usage.get("completion_tokens_details")
        if not isinstance(details, dict) or not isinstance(output, dict):
            return fallback
        values = (
            details.get("text_tokens"),
            details.get("audio_tokens"),
            usage.get("completion_tokens"),
            usage.get("prompt_tokens"),
            output.get("audio_tokens"),
        )
        if any(type(v) is not int or v < 0 for v in values):
            return fallback
        text, audio, completion, total, output_audio = values
        if text + audio != total or output_audio != 0:
            return fallback
        rates = profile.price
        estimate = (
            Decimal(text) * Decimal(rates.usd_per_million_input_text_tokens)
            + Decimal(audio) * Decimal(rates.usd_per_million_input_audio_tokens)
            + Decimal(completion) * Decimal(rates.usd_per_million_output_text_tokens)
        )
    else:
        values = (usage.get("total_input_tokens"), usage.get("total_output_tokens"))
        if any(type(v) is not int or v < 0 for v in values):
            return fallback
        estimate = Decimal(values[0]) * Decimal(profile.price.usd_per_million_input_tokens) + Decimal(
            values[1]
        ) * Decimal(profile.price.usd_per_million_output_tokens)
    return dict(status="provider_token_estimate_not_invoice", usd=str(estimate / 1_000_000))


def realtime_cost(profile, usage, fallback):
    details, output = usage.get("input_token_details"), usage.get("output_token_details")
    if not isinstance(details, dict) or not isinstance(output, dict):
        return fallback
    values = (
        details.get("text_tokens"),
        details.get("audio_tokens"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        output.get("audio_tokens"),
        details.get("image_tokens", 0),
    )
    if any(type(v) is not int or v < 0 for v in values):
        return fallback
    text, audio, total, completion, audio_out, images = values
    if text + audio != total or audio_out or images:
        return fallback
    price = profile.price
    estimate = (
        Decimal(text) * Decimal(price.usd_per_million_input_text_tokens)
        + Decimal(audio) * Decimal(price.usd_per_million_input_audio_tokens)
        + Decimal(completion) * Decimal(price.usd_per_million_output_text_tokens)
    ) / 1_000_000
    return dict(status="provider_token_estimate_not_invoice", usd=str(estimate))
