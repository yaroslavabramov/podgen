"""Matrix semantics shared by run status and saved exports."""


def full_comparison_complete(manifest, completed):
    config = manifest["configuration"]
    counts = config.get("expected_voices_per_provider", {p: 2 for p in config["providers"]})
    return (
        len(config["providers"]) == 5
        and len(config["fragments"]) == 2
        and all(counts[p] == 2 for p in config["providers"])
        and completed == 20
    )
