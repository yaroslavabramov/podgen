from app.schemas.generation import CostEstimateResponse


# Pricing per 1M tokens (approximate)
SONNET_INPUT_PRICE = 3.0
SONNET_OUTPUT_PRICE = 15.0
HAIKU_INPUT_PRICE = 0.80
HAIKU_OUTPUT_PRICE = 4.0
EMBEDDING_PRICE = 0.13  # per 1M tokens
ELEVENLABS_PRICE_PER_CHAR = 0.00003  # approximate


def estimate_cost(duration_target: int, source_count: int) -> CostEstimateResponse:
    """Estimate generation cost based on duration and source count."""
    duration_minutes = duration_target / 60

    # Estimated tokens per phase (rough approximation)
    # More sources = more ingestion/research tokens
    source_factor = max(1, source_count / 3)

    # Phase 0: Show setup ~2k tokens
    # Phase 2: Decomposition ~5k tokens
    # Phase 3: Research ~10k * source_factor
    # Phase 4: Claims ~8k
    # Phase 5: Planning ~10k * (duration_minutes / 15)
    # Phase 6: Writing ~20k * (duration_minutes / 15)
    # Phase 7: Shownotes ~3k

    sonnet_input_tokens = int((
        2000 +  # Phase 0
        5000 +  # Phase 2
        10000 * source_factor +  # Phase 3
        8000 +  # Phase 4
        10000 * (duration_minutes / 15) +  # Phase 5
        20000 * (duration_minutes / 15) +  # Phase 6
        3000  # Phase 7
    ))

    sonnet_output_tokens = int(sonnet_input_tokens * 0.3)
    haiku_tokens = int(sonnet_input_tokens * 0.2)  # Scout, evaluator, shownotes

    llm_cost = (
        (sonnet_input_tokens / 1_000_000) * SONNET_INPUT_PRICE +
        (sonnet_output_tokens / 1_000_000) * SONNET_OUTPUT_PRICE +
        (haiku_tokens / 1_000_000) * HAIKU_INPUT_PRICE +
        (haiku_tokens * 0.3 / 1_000_000) * HAIKU_OUTPUT_PRICE
    )

    # TTS: ~150 words/minute, ~5 chars/word
    total_chars = int(duration_minutes * 150 * 5)
    tts_cost = total_chars * ELEVENLABS_PRICE_PER_CHAR

    return CostEstimateResponse(
        estimated_llm_cost=round(llm_cost, 2),
        estimated_tts_cost=round(tts_cost, 2),
        estimated_total=round(llm_cost + tts_cost, 2),
        estimated_minutes=duration_minutes,
    )
