"""One operator-selected break between planning clearance and the first writer request."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from podgen.domain.errors import Conflict
from podgen.persistence.database import query

from .planning import clearance_ref


def _mark_id(run_id, event):
    return uuid5(run_id, "planning-checkpoint/1:" + event)


@dataclass
class PlanningCheckpoint:
    run_id: UUID
    paused: datetime | None = None
    resumed: datetime | None = None

    @classmethod
    def load(cls, journal, run_id):
        started, ended = _mark_id(run_id, "paused"), _mark_id(run_id, "resumed")
        with journal.repo.database.read_transaction() as tx:
            journal.authorize(tx)
            rows = (
                query(
                    tx,
                    """SELECT record_id, payload FROM runtime_records
                   WHERE project_id=:p AND record_id IN (:started,:ended) AND record_kind='tool_execution'""",
                    p=journal.project,
                    started=started,
                    ended=ended,
                )
                .mappings()
                .all()
            )
        times = {}
        for row in rows:
            event = "paused" if row["record_id"] == started else "resumed"
            payload = row["payload"]
            if (
                payload.get("contract") != "planning-checkpoint/1"
                or payload.get("run_id") != str(run_id)
                or payload.get("event") != event
                or payload.get("clearance_ref") != clearance_ref(run_id).model_dump(mode="json")
            ):
                raise Conflict("invalid_planning_checkpoint")
            times[event] = datetime.fromisoformat(payload["at"])
        if "resumed" in times and ("paused" not in times or times["resumed"] < times["paused"]):
            raise Conflict("invalid_planning_checkpoint")
        return cls(run_id, **times)

    def wait_time(self, now):
        return (self.resumed or now) - self.paused if self.paused else timedelta(0)

    def mark(self, journal, event, now):
        if event == "paused" and self.paused is not None:
            return
        if event == "resumed" and (self.paused is None or self.resumed is not None):
            return
        journal.record(
            dict(
                contract="planning-checkpoint/1",
                run_id=str(self.run_id),
                event=event,
                at=now.isoformat(),
                clearance_ref=clearance_ref(self.run_id).model_dump(mode="json"),
            ),
            record_id=_mark_id(self.run_id, event),
        )
        setattr(self, event, now)


def result(command, plan, totals):
    return dict(
        run_id=str(command.run_id),
        case_id=command.case_id,
        status="plan_ready",
        accepted_plan_ref=plan["accepted_plan_ref"],
        accepted_plan_hash=plan["accepted_plan_hash"],
        plan_review_ref=plan["plan_review_ref"],
        planning_clearance_ref=plan["planning_clearance_ref"],
        token_usage_or_reservation=totals,
        semantic_acceptance=False,
        production_ready=False,
    )
