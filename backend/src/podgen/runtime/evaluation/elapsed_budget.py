"""Absolute elapsed ceilings authorized on resume; frozen run inputs never change."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.database import query

from .contracts import RunFailure

CONTRACT = "resume-elapsed-authorization/1"


def _identity(run_id, ceiling):
    return uuid5(run_id, f"{CONTRACT}:{ceiling}")


def _valid_ceiling(value):
    return type(value) is int and 0 < value < 2**63


@dataclass(frozen=True)
class ElapsedBudget:
    run_id: UUID
    created_at: datetime
    ceiling: int
    authorizations: tuple = ()

    @classmethod
    def load(cls, journal, manifest, *, requested=None):
        """The caller holds the run lock when requesting a new immutable grant."""
        run_id = UUID(manifest["command"]["run_id"])
        original = manifest["command"]["limits"]["max_elapsed_seconds"]
        if requested is not None and (not _valid_ceiling(requested) or requested <= original):
            raise ContractError("elapsed_allowance_invalid")
        run = journal.run(run_id)
        manifest_hash = digest(manifest)
        manifest_ref = RuntimeRef(record_kind="context_assembly", record_id=run["manifest_id"])
        with journal.repo.database.read_transaction() as tx:
            journal.authorize(tx)
            identities = list(
                query(
                    tx,
                    """SELECT record_id FROM runtime_records
                WHERE project_id=:p AND record_kind='tool_execution'
                  AND podgen_runtime_reference_document(payload)->>'contract'='resume-elapsed-authorization/1'
                  AND podgen_runtime_reference_document(payload)->>'run_id'=:run""",
                    p=journal.project,
                    run=str(run_id),
                ).scalars()
            )
        grants = []
        for offset in range(0, len(identities), 64):
            batch = identities[offset : offset + 64]
            loaded = journal.read_many(
                [RuntimeRef(record_kind="tool_execution", record_id=identity) for identity in batch]
            )
            for identity, grant in zip(batch, loaded, strict=True):
                value = grant.get("max_elapsed_seconds")
                if not _valid_ceiling(value) or identity != _identity(run_id, value):
                    raise Conflict("elapsed_authorization_mismatch")
                try:
                    authorized_at = datetime.fromisoformat(grant["authorized_at"])
                    if authorized_at.tzinfo is None:
                        raise ValueError()
                except (KeyError, ValueError, TypeError):
                    raise Conflict("elapsed_authorization_mismatch") from None
                grants.append(grant)
        ceiling = original
        for grant in sorted(grants, key=lambda value: value["max_elapsed_seconds"]):
            value = grant["max_elapsed_seconds"]
            if (
                grant.get("contract") != CONTRACT
                or grant.get("run_id") != str(run_id)
                or grant.get("actor_id") != str(run["actor_id"])
                or grant.get("manifest_ref") != manifest_ref.model_dump(mode="json")
                or grant.get("manifest_hash") != manifest_hash
                or grant.get("previous_ceiling") != ceiling
                or value <= ceiling
            ):
                raise Conflict("elapsed_authorization_mismatch")
            ceiling = value
        if requested is not None:
            if run["actor_id"] != journal.repo.actor_id:
                raise AccessDenied()
            if requested < ceiling:
                raise ContractError("elapsed_allowance_invalid")
            if requested > ceiling:
                grant = dict(
                    contract=CONTRACT,
                    run_id=str(run_id),
                    actor_id=str(journal.repo.actor_id),
                    manifest_ref=manifest_ref.model_dump(mode="json"),
                    manifest_hash=manifest_hash,
                    previous_ceiling=ceiling,
                    max_elapsed_seconds=requested,
                    authorized_at=datetime.now(timezone.utc).isoformat(),
                )
                journal.record(grant, record_id=_identity(run_id, requested))
                grants.append(grant)
                ceiling = requested
        return cls(run_id, datetime.fromisoformat(manifest["created_at"]), ceiling, tuple(grants))

    def check(self, checkpoint, now):
        elapsed = (now - self.created_at - checkpoint.wait_time(now)).total_seconds()
        if elapsed > self.ceiling:
            raise RunFailure("run_elapsed_budget_exceeded", run_id=self.run_id)
