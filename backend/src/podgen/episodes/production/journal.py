"""Bounded head reads, immutable configuration, CAS transitions and a single runner lease."""

from contextlib import contextmanager

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.database import json_text, one, query

from .control import ProductionPaused, ProductionSuperseded

# Automated work is over; only review steps may change an assembled or accepted run.
FINISHED = ("assembled", "accepted")


class ProductionJournal:
    def __init__(self, repo, project, run_id):
        self.repo, self.project, self.run_id = repo, project, run_id

    def _row(self, tx, *, lock=False):
        row = one(
            tx,
            """SELECT r.* FROM episode_production_runs r
            JOIN project_members m ON m.project_id=r.project_id AND m.actor_id=:actor
            WHERE r.project_id=:p AND r.run_id=:r AND r.actor_id=:actor
              AND m.role IN ('owner','evaluator','operator')"""
            + (" FOR UPDATE OF r" if lock else ""),
            p=self.project,
            r=self.run_id,
            actor=self.repo.actor_id,
        )
        if row is None or self.repo.purpose != "evaluation":
            raise AccessDenied()
        return dict(row)

    def read(self):
        with self.repo.database.read_transaction() as tx:
            return self._row(tx)

    def prepare(self, config):
        if config.run_id != self.run_id:
            raise ContractError("episode_production_run_mismatch")
        raw = config.model_dump(mode="json")
        with self.repo.database.transaction() as tx:
            if self.repo.purpose != "evaluation" or self.repo._acl(tx, self.project, write=True) not in (
                "owner",
                "evaluator",
                "operator",
            ):
                raise AccessDenied()
            query(
                tx,
                """INSERT INTO episode_production_runs
                (project_id,run_id,actor_id,configuration,configuration_hash)
                VALUES(:p,:r,:actor,CAST(:config AS jsonb),:hash) ON CONFLICT DO NOTHING""",
                p=self.project,
                r=self.run_id,
                actor=self.repo.actor_id,
                config=json_text(raw),
                hash=digest(raw),
            )
            row = self._row(tx)
            if row["configuration_hash"] != digest(raw):
                raise Conflict("episode_run_configuration_changed")
        return row

    def _write(self, tx, row, **changes):
        if all(row[key] == value for key, value in changes.items()):
            return row
        revision = row["revision"] + 1
        fields = dict(row, **changes, revision=revision)
        updated = one(
            tx,
            """UPDATE episode_production_runs SET revision=:revision,epoch=:epoch,
            desired_state=:desired_state,status=:status,phase=:phase,
            progress=CAST(:progress AS jsonb),updated_at=now()
            WHERE project_id=:project_id AND run_id=:run_id AND revision=:previous RETURNING *""",
            **{**fields, "progress": json_text(fields["progress"]), "previous": row["revision"]},
        )
        if updated is None:
            raise ProductionSuperseded()
        query(
            tx,
            """INSERT INTO episode_production_events(project_id,run_id,revision,payload)
            VALUES(:p,:r,:revision,CAST(:payload AS jsonb))""",
            p=self.project,
            r=self.run_id,
            revision=revision,
            payload=json_text(
                dict(
                    phase=fields["phase"],
                    status=fields["status"],
                    desired_state=fields["desired_state"],
                    epoch=fields["epoch"],
                    error_code=fields["progress"].get("error_code"),
                )
            ),
        )
        return dict(updated)

    def request(self, desired):
        if desired not in ("running", "paused"):
            raise ContractError("episode_invalid_desired_state")
        with self.repo.database.transaction() as tx:
            row = self._row(tx, lock=True)
            if row["status"] in FINISHED:
                return row
            status = row["status"]
            if desired == "paused" and status not in (*FINISHED, "paused"):
                status = "pause_requested"
            return self._write(tx, row, desired_state=desired, status=status)

    def claim(self):
        with self.repo.database.transaction() as tx:
            row = self._row(tx, lock=True)
            if row["status"] in FINISHED:
                return row
            if row["desired_state"] == "paused":
                raise ProductionPaused()
            return self._write(
                tx, row, epoch=row["epoch"] + 1, status="waiting" if row["status"] == "waiting" else "running"
            )

    def check_dispatch(self, epoch):
        row = self.read()  # Exactly one bounded read; polling never appends records.
        if row["epoch"] != epoch:
            raise ProductionSuperseded()
        if row["desired_state"] == "paused":
            raise ProductionPaused()

    def transition(self, epoch, progress, phase, status):
        with self.repo.database.transaction() as tx:
            row = self._row(tx, lock=True)
            if row["epoch"] != epoch:
                raise ProductionSuperseded()
            if row["desired_state"] == "paused" and status in ("running", "waiting"):
                status = "pause_requested"
            return self._write(tx, row, progress=progress, phase=phase, status=status)

    def review(self, progress, status):
        """Record a review step of an assembled master; acceptance is final."""
        if status not in ("assembled", "accepted"):
            raise ContractError("episode_invalid_review_status")
        with self.repo.database.transaction() as tx:
            row = self._row(tx, lock=True)
            if (
                row["status"] not in ("assembled", "accepted")
                or row["status"] == "accepted"
                and status != "accepted"
            ):
                raise Conflict("episode_review_state_changed")
            return self._write(tx, row, progress=progress, status=status)

    @contextmanager
    def lock(self):
        with self.repo.database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as tx:
            self._row(tx)
            key = f"episode-production:{self.project}:{self.run_id}"
            if not one(tx, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) held", key=key)["held"]:
                raise Conflict("episode_production_busy")
            try:
                yield
            finally:
                query(tx, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=key)
