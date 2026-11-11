"""Show seed provenance, global revision numbering and cumulative usage without inventing paid calls."""

from pathlib import Path

from .budget import own_usage, split_usage
from .source import KEY, offset, seed


def export(journal, run_id, manifest, write_json):
    binding = manifest[KEY]
    script, _, readings, _ = seed(journal, manifest)
    write_json(Path("editorial-continuation.json"), binding)
    write_json(Path("editorial-seed-script.json"), script)
    write_json(Path("editorial-seed-readings.json"), readings)
    charged = own_usage(journal, run_id)
    total = {key: binding["inherited_usage"][key] + charged[key] for key in charged}
    revisions = sum(
        journal.invocation(run_id, stage) is not None
        and journal.invocation(run_id, stage)["status"] == "completed"
        for stage in manifest["stages"]
        if stage.startswith("revise-")
    )
    return dict(
        editorial_continuation=binding,
        usage_ledger=split_usage(manifest, total),
        revisions_completed=offset(manifest) + revisions,
        max_total_revisions=binding["max_total_revisions"],
    )
