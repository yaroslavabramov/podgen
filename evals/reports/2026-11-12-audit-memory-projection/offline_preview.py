"""Offline diagnostic only: read the frozen run, compare prospective memory without creating a run."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import Database
from podgen.persistence.editorial_inputs import (
    generation_repo,
    load_editorial_materials,
)
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.reading_audit.source import prepare
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.wire_budget import visible_sizes
from podgen.writing.reading_audit.memory_projection import KEY, VERSION

root = Path.cwd()
folder = root / "tmp/review-audit-release/saved-cycle/export"
m = json.loads((folder / "manifest.json").read_text())
command = WritingRunCommand.model_validate(m["command"])
state = json.loads((root / "tmp/behavior-qualification/db-state.json").read_text())
url = (
    "postgresql+psycopg://runner@/postgres?host="
    + state["socket"]
    + "&port=55449&options="
    + quote("-c default_transaction_read_only=on")
)
db = Database(url)
try:
    repo = Repository(
        db,
        FileObjectStore(root / "tmp/behavior-qualification/objects"),
        command.actor_id,
        purpose="evaluation",
    )
    journal = EvaluationJournal(repo, command.project_id)
    generation = generation_repo(repo)
    materials = load_editorial_materials(
        generation,
        command.project_id,
        command.author_ref,
        case_id=command.case_id,
        fixed_ref=command.fixed_plan_input_ref,
    )
    stage = "fact-review-1"
    uid = command.run_id
    snapshot = load_preview_snapshot(journal, generation, uid, stage, materials)
    profile = load_profile(
        generation, command.project_id, command.reviewer_profile_ref, stage
    )
    old = preview_request(
        uid, stage, profile, materials, snapshot=snapshot, draft=snapshot.script
    )
    original = journal.request(journal.attempts(uid, stage)[0])
    assert old == original
    prospective = deepcopy(snapshot.manifest)
    prospective[KEY] = VERSION
    revision = journal.invocation(uid, "revise-1")
    ref = RuntimeRef(
        record_kind="tool_execution", record_id=revision["output_record_id"]
    ).model_dump(mode="json")
    projected = prepare(
        journal, uid, stage, prospective, materials, snapshot.script, ref
    )
    new = preview_request(
        uid,
        stage,
        profile,
        materials,
        snapshot=replace(
            snapshot, manifest=prospective, reading_audit_context=projected.view
        ),
        draft=snapshot.script,
    )
    before = request_payload(old)
    after = request_payload(new)
    assert {k: v for k, v in before.items() if k != "audit_context"} == {
        k: v for k, v in after.items() if k != "audit_context"
    }
    assert (
        old["instructions"] == new["instructions"]
        and old["text"] == new["text"]
        and old["tools"] == new["tools"]
    )
    assert (
        len(before["audit_context"]["previous_decisions"])
        == len(after["audit_context"]["previous_decisions"])
        == 19
    )
    for old_d, new_d in zip(
        before["audit_context"]["previous_decisions"],
        after["audit_context"]["previous_decisions"],
        strict=True,
    ):
        assert {k: v for k, v in old_d.items() if k != "source_refs"} == {
            k: v for k, v in new_d.items() if k != "source_refs"
        }
    result = dict(
        mode="offline_read_only_prospective_projection",
        source_run_id=str(uid),
        source_stage=stage,
        memory_projection=VERSION,
        old_visible_bytes=visible_sizes(old),
        new_visible_bytes=visible_sizes(new),
        saved_visible_bytes=visible_sizes(old)["total"] - visible_sizes(new)["total"],
        old_retained_evidence=len(before["audit_context"]["previous_evidence"]),
        new_retained_evidence=len(after["audit_context"]["previous_evidence"]),
        reused_packet_references=len(projected.binding["reused_packet_evidence"]),
        previous_decisions=19,
        original_frozen_request_exact=True,
        non_memory_payload_exact=True,
        instructions_schema_tools_exact=True,
        full_prior_reports_unchanged=True,
        frozen_outcome_replay=read_outcome(journal, uid)["status"],
        api_called=False,
        frozen_manifest_changed=False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    db.engine.dispose()
