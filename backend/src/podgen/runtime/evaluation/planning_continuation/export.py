"""Export ancestry and exact seed outputs without presenting them as new paid stages."""

from pathlib import Path

from podgen.domain.contracts.common import RuntimeRef

from .source import KEY


def seed_records(journal, manifest):
    binding = manifest[KEY]
    return (
        journal.read(RuntimeRef.model_validate(binding["candidate_plan_ref"])),
        journal.read(RuntimeRef.model_validate(binding["plan_review_ref"])),
    )


def export(journal, manifest, write_json):
    if KEY not in manifest:
        return
    candidate, review = seed_records(journal, manifest)
    write_json(Path("planning-continuation.json"), manifest[KEY])
    write_json(Path("planning-seed-plan.json"), candidate["output"])
    write_json(Path("planning-seed-review.json"), review["output"])
