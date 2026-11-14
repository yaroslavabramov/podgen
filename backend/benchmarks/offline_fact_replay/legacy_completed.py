"""Open and resume a genuine completed legacy run; require byte-exact old exports."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from metrics import Metrics
from storage_audit import snapshot

from podgen.domain.canonical import digest
from podgen.persistence.database import Database
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects
from podgen.runtime.evaluation.fact_source_experiment.service import run
from podgen.runtime.evaluation.journal import EvaluationJournal


def files(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def no_provider(profile):
    raise AssertionError("completed_legacy_resume_must_not_call_provider")


state = json.loads(Path(os.environ["PODGEN_REPLAY_STATE"]).read_text())
output = Path(os.environ["PODGEN_REPLAY_OUT"])
expected = files(Path(os.environ["PODGEN_LEGACY_EXPORT"]))
assert expected, "legacy_export_fixture_missing"
database = Database(state["database_url"])
objects = MeasuredObjects(Path(state["objects"]))
repo = Repository(database, objects, UUID(state["actor"]), purpose="evaluation")
generation = Repository(database, objects, repo.actor_id, purpose="generation")
journal = EvaluationJournal(repo, UUID(state["project"]))
run_id = UUID("a07cc6d1-c399-5979-a698-084ece565c47")
try:
    before = snapshot(database, journal.project, run_id)
    assert before["storage"] == "legacy", "fixture_is_not_legacy"
    invocation = journal.invocation(run_id, "fact-review")
    assert invocation["status"] == "completed", "fixture_is_not_completed"
    original = journal.output(invocation)
    approved = digest(journal.request(invocation))
    metrics = Metrics(database, objects)
    fixture_time = datetime.fromisoformat(journal.manifest(run_id)["created_at"]) + timedelta(seconds=1)

    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixture_time.astimezone(tz) if tz is not None else fixture_time.replace(tzinfo=None)

    # Reproduce the archived run's deadline without modifying immutable history.
    # Performance measurements use real monotonic time, unaffected by this clock.
    with patch("podgen.runtime.evaluation.fact_source_experiment.service.datetime", FixtureClock):
        run(
            repo,
            generation,
            journal,
            UUID(state["run_id"]),
            run_id,
            no_provider,
            approved,
            metrics,
            output,
        )
    assert journal.output(journal.invocation(run_id, "fact-review")) == original
    after = snapshot(database, journal.project, run_id)
    assert before == after, "completed_legacy_resume_modified_storage"
    current = files(output / "export")
    assert expected.keys() == current.keys(), "legacy_export_inventory_changed"
    changed = [name for name in expected if expected[name] != current[name]]
    assert not changed, ("legacy_export_bytes_changed", changed)
    (output / "legacy-completed.json").write_text(
        json.dumps(
            dict(
                storage_unchanged=True,
                provider_calls=0,
                fixture_wall_clock=fixture_time.isoformat(),
                byte_exact_export_files=len(current),
                metrics=metrics.snapshot(),
            ),
            indent=2,
        )
    )
finally:
    database.engine.dispose()
