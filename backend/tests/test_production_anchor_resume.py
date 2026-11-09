"""The omitted historical strict value is restored from the frozen command, not new-run defaults."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import service
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.run_configuration import production_command
from podgen.writing.behavior import PRODUCTION


def test_production_strict_and_bounded_resume_restore_frozen_policy_but_reject_explicit_changes(monkeypatch):
    actor, project = uuid4(), uuid4()
    profile = ArtifactRef(kind="ModelProfile", artifact_id=uuid4(), revision_id=uuid4())
    repo = SimpleNamespace(actor_id=actor, database=None, objects=None)
    monkeypatch.setattr(service, "Repository", lambda *args, **kwargs: None)

    for policy in ("strict", "bounded-selection-v1"):
        original = production_command(
            WritingRunCommand(
                run_id=uuid4(),
                project_id=project,
                actor_id=actor,
                case_id="saved-production",
                author_ref=RuntimeRef(record_kind="user_input", record_id=uuid4()),
                writer_profile_ref=profile,
                reviewer_profile_ref=profile,
                behavior_profile=PRODUCTION,
                review_anchor_policy=policy,
            )
        )
        frozen = dict(behavior_profile=PRODUCTION, command=command_payload(original))
        assert ("review_anchor_policy" in frozen["command"]) == (policy != "strict")
        restored = WritingRunCommand.model_validate(frozen["command"])
        resumed = production_command(restored, frozen_manifest=frozen)
        assert resumed.review_anchor_policy == policy
        assert command_payload(resumed) == frozen["command"]
        fingerprint = digest(frozen["command"])
        assert digest(command_payload(resumed)) == fingerprint
        journal = SimpleNamespace(
            lock=lambda *args: nullcontext(),
            run=lambda *args, saved=fingerprint: dict(input_fingerprint=saved),
            manifest=lambda *args, saved=frozen: saved,
        )
        monkeypatch.setattr(service, "EvaluationJournal", lambda *args, current=journal: current)
        changed = restored.model_copy(
            update=dict(review_anchor_policy="bounded-selection-v1" if policy == "strict" else "strict")
        )
        with pytest.raises(Conflict, match="run_id_reused_with_different_input"):
            service.WritingEvaluation(repo, lambda *args: pytest.fail("No provider may be constructed")).run(
                changed
            )
        assert production_command(changed, frozen_manifest=frozen).review_anchor_policy != policy
