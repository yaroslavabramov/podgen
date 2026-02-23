"""Duration Fill Strategy: determine which content layers to activate."""


# Layer estimates in minutes per unit
LAYER_ESTIMATES = {
    "L1_definitions": 3,
    "L2_core_mechanisms": 5,
    "L3_history": 7,
    "L4_case_studies": 4,
    "L5_edge_cases": 4,
    "L6_debates": 6,
    "L7_adjacent": 4,
    "L8_takeaways": 2,
}

# Layers in priority order
LAYER_ORDER = [
    "L1_definitions",
    "L2_core_mechanisms",
    "L3_history",
    "L4_case_studies",
    "L5_edge_cases",
    "L6_debates",
    "L7_adjacent",
    "L8_takeaways",
]


def compute_fill_layers(
    duration_minutes: float,
    branch_count: int,
    audience_profile: dict,
) -> list[str]:
    """Determine which duration fill layers to activate.

    Args:
        duration_minutes: Target podcast duration in minutes.
        branch_count: Number of top-level research branches.
        audience_profile: Audience profile from ShowBible.

    Returns:
        List of layer names to activate, in order.
    """
    # Estimate core material coverage: ~3 min per branch (rough heuristic)
    core_minutes = branch_count * 3
    remaining = duration_minutes - core_minutes

    if remaining <= 0:
        # Core material alone exceeds target — only use L1 + L2
        return ["L1_definitions", "L2_core_mechanisms"]

    audience_level = audience_profile.get("level", "casual")
    active_layers = []
    filled = 0

    for layer in LAYER_ORDER:
        if filled >= remaining:
            break

        # Guardrail: professional audience → minimal adjacent expansion
        if layer == "L7_adjacent" and audience_level == "professional":
            continue

        # Guardrail: limit adjacent topics by duration
        if layer == "L7_adjacent":
            max_adjacent = 2 if duration_minutes <= 30 else 4 if duration_minutes <= 60 else 6
            est = min(LAYER_ESTIMATES[layer] * max_adjacent, remaining - filled)
        else:
            est = LAYER_ESTIMATES[layer]

        active_layers.append(layer)
        filled += est

    return active_layers
