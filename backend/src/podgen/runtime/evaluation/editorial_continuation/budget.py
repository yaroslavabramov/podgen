"""Cumulative charged usage, including ancestors that stopped in planning."""

from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError

from ..usage import attempt_usage


def own_usage(journal, run_id):
    value = dict(input=0, output=0)
    for stage in journal.manifest(run_id)["stages"]:
        for row in journal.attempts(run_id, stage):
            record = journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"])
            )
            profile = journal.repo.get(
                journal.project, ArtifactRef.model_validate(record["context"]["profile_ref"])
            ).payload
            charged = attempt_usage(journal, row, profile["output_limit"])
            for key in value:
                value[key] += charged[key]
    return value


def inherited_usage(journal, manifest, outcome):
    totals = dict(outcome["token_usage_or_reservation"])
    if "editorial_continuation" in manifest:
        return totals, manifest["editorial_continuation"]["planning_ancestry_usage"]
    lineage, seen = [], set()
    while "planning_continuation" in manifest:
        parent = UUID(manifest["planning_continuation"]["parent_run_id"])
        if parent in seen:
            raise ContractError("editorial_continuation_ancestry_cycle")
        seen.add(parent)
        manifest = journal.manifest(parent)
        value = own_usage(journal, parent)
        lineage.append(dict(run_id=str(parent), token_usage_or_reservation=value))
        for key in totals:
            totals[key] += value[key]
    return totals, lineage


def split_usage(manifest, total):
    inherited = manifest["editorial_continuation"]["inherited_usage"]
    return dict(
        inherited=dict(inherited),
        own={key: total[key] - inherited[key] for key in inherited},
        total=dict(total),
    )
