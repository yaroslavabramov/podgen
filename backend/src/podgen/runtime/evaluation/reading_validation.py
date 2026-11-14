"""One full structural validation; immutable content bindings never cache access."""

from dataclasses import dataclass
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.database import json_text, one, query
from podgen.persistence.execution.inputs import _publish
from podgen.persistence.read_authorization import _read_connection

from .reading_cycle import outcome_ref

VERSION = "validated-reading-outcome/1"


@dataclass(frozen=True)
class ValidatedOutcome:
    run_id: object
    outcome: dict
    binding: dict

    def authorize(self, journal):
        """Reuse immutable content only; obtain current access at this boundary."""
        with journal.read_operation():
            if _stored(journal, self.run_id) != self.binding:
                raise Conflict("validated_reading_outcome_binding_mismatch")
            if digest(self.outcome) != self.binding["records"][0]["sha256"]:
                raise Conflict("validated_reading_outcome_binding_mismatch")
            journal.authorize_many([RuntimeRef.model_validate(row["ref"]) for row in self.binding["records"]])


def _stored(journal, run_id):
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        row = one(
            tx,
            """SELECT r.actor_id,r.manifest_id,v.binding FROM evaluation_writing_runs r
            LEFT JOIN writing_outcome_validations v ON v.project_id=r.project_id
              AND v.run_id=r.run_id AND v.validator_version=:v
            WHERE r.project_id=:p AND r.run_id=:r""",
            p=journal.project,
            r=run_id,
            v=VERSION,
        )
        if row and row["actor_id"] != journal.repo.actor_id:
            raise AccessDenied()
        if row and row["binding"] is not None:
            value = row["binding"]
            if (
                value.get("contract") != VERSION
                or value.get("run_id") != str(run_id)
                or len(value.get("records", [])) != 5
                or value["records"][1]["ref"]
                != RuntimeRef(record_kind="context_assembly", record_id=row["manifest_id"]).model_dump(
                    mode="json"
                )
            ):
                raise Conflict("validated_reading_outcome_binding_mismatch")
        return row["binding"] if row else None


def _records(journal, run_id, outcome=None, expected=None):
    ref = outcome_ref(run_id)
    manifest_ref = (
        RuntimeRef.model_validate(expected["records"][1]["ref"])
        if expected
        else RuntimeRef(record_kind="context_assembly", record_id=journal.run(run_id)["manifest_id"])
    )
    # Read outcome/manifest once; then the exact script and final independent
    # readings as one batch. Both batches share only this local read snapshot.
    if outcome is None:
        outcome, manifest = journal.read_many([ref, manifest_ref])
    else:
        journal.authorize_many([ref])
        manifest = journal.read(manifest_ref)
    refs = [RuntimeRef.model_validate(outcome["script_ref"])] + [
        RuntimeRef.model_validate(outcome["reading_refs"][role]) for role in ("reader", "fact")
    ]
    script, reader, fact = journal.read_many(refs)
    records = [outcome, manifest, script, reader, fact]
    all_refs = [ref, manifest_ref, *refs]
    binding = dict(
        contract=VERSION,
        run_id=str(run_id),
        status=outcome["status"],
        records=[
            dict(ref=reference.model_dump(mode="json"), sha256=digest(record))
            for reference, record in zip(all_refs, records, strict=True)
        ],
    )
    if (
        outcome["run_id"] != str(run_id)
        or outcome["script_hash"] != digest(script["output"])
        or outcome["output"] != script["output"]
    ):
        raise Conflict("independent_final_script_mismatch")
    return outcome, binding


def certify(journal, run_id):
    """Adopt legacy outcomes once, or certify a newly completed native cycle."""
    from .reading_audit_replay import audit_outcome

    # A separate lease also works inside the writer's existing run lease.
    with journal.lock(uuid5(run_id, VERSION)):
        with journal.read_operation():
            saved = _stored(journal, run_id)
            if saved is not None:
                outcome, binding = _records(journal, run_id, expected=saved)
                if binding != saved:
                    raise Conflict("validated_reading_outcome_binding_mismatch")
                return outcome
        outcome = audit_outcome(journal, run_id)
        if outcome is None:
            return None
        with journal.read_operation():
            current, binding = _records(journal, run_id)
            if current != outcome:
                raise Conflict("validated_reading_outcome_changed_during_validation")

        def publish(tx):
            journal.authorize(tx)
            journal.repo._acl(tx, journal.project, write=True)
            journal.authorize_many([RuntimeRef.model_validate(row["ref"]) for row in binding["records"]])
            query(
                tx,
                """INSERT INTO writing_outcome_validations VALUES(:p,:r,:v,CAST(:b AS jsonb),now())
                ON CONFLICT DO NOTHING""",
                p=journal.project,
                r=run_id,
                v=VERSION,
                b=json_text(binding),
            )
            stored = one(
                tx,
                """SELECT binding FROM writing_outcome_validations
                WHERE project_id=:p AND run_id=:r AND validator_version=:v""",
                p=journal.project,
                r=run_id,
                v=VERSION,
            )
            if stored["binding"] != binding:
                raise Conflict("validated_reading_outcome_binding_mismatch")

        _publish(journal.repo, journal.project, publish)
        return outcome


def read_validated(journal, run_id):
    value = read_descriptor(journal, run_id)
    return value.outcome if value else None


def read_descriptor(journal, run_id, *, outcome=None):
    with journal.read_operation():
        saved = _stored(journal, run_id)
        if saved is not None:
            outcome, binding = _records(journal, run_id, outcome, expected=saved)
            if binding != saved:
                raise Conflict("validated_reading_outcome_binding_mismatch")
            return ValidatedOutcome(run_id, outcome, binding)
        with _read_connection(journal.repo) as tx:
            journal.authorize(tx)
            exists = one(
                tx,
                "SELECT 1 FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=journal.project,
                r=outcome_ref(run_id).record_id,
            )
        if not exists:
            return None
    outcome = certify(journal, run_id)
    if outcome is None:
        return None
    with journal.read_operation():
        binding = _stored(journal, run_id)
        return ValidatedOutcome(run_id, outcome, binding)
