"""Explicit selected X reports reach the accepted plan's author in full, separately from M/E."""

from hashlib import sha256
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.research_annex import attach, restore
from podgen.runtime.models.research_tools import ResearchTools
from podgen.sources.material_requests import SourceAdmission
from podgen.writing.source_tools import SourceCorpus
from tests.test_research_navigation_contract import _plan, _profile
from tests.test_research_navigation_contract import research_materials as research_materials


@pytest.mark.parametrize("selector_stage", ["plan", "plan-review", None])
def test_full_selected_supplemental_report_is_bound_to_exact_plan_and_writer(
    research_materials, selector_stage, monkeypatch
):
    text = "Full supplemental account ёжик 🚀.\n" * 1000
    report = SourceAdmission(
        "X1",
        "Targeted question",
        text,
        sha256(text.encode()).hexdigest(),
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        dict(kind="research_report", reported_source_map={"S1": "S99"}),
    )
    unselected = SourceAdmission(
        "X2",
        "Unused question",
        "UNSELECTED",
        sha256(b"UNSELECTED").hexdigest(),
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        dict(kind="research_report"),
    )
    research_materials.runtime_admissions.extend((report, unselected))
    run, invocation = uuid4(), uuid4()
    row = dict(invocation_id=invocation, status="completed", attempt_kind="role", output_record_id=uuid4())
    review_row = dict(row, invocation_id=uuid4(), output_record_id=uuid4())
    plan = _plan(research_materials)
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    event = dict(
        event_kind="source_tool_result",
        data=dict(
            name="select_research_result",
            arguments=dict(result_id="X1", reason="The chosen account requires this report."),
            result=dict(selected=True),
        ),
    )

    class Journal:
        records = {}
        event_reads = 0

        def read_operation(self):
            from contextlib import nullcontext

            return nullcontext()

        def run(self, _):
            return True

        def manifest(self, _):
            return {}

        def invocation(self, _, stage):
            return row if stage == "plan" else review_row if stage == "plan-review" else None

        def output(self, current):
            if current["invocation_id"] == invocation:
                return dict(run_id=str(run), output=plan)
            return dict(
                run_id=str(run),
                output=dict(decision="accept"),
                candidate_plan_hash=digest(plan),
                candidate_plan_ref=RuntimeRef(
                    record_kind="tool_execution", record_id=row["output_record_id"]
                ).model_dump(mode="json"),
            )

        def events(self, current):
            self.event_reads += 1
            chosen = invocation if selector_stage == "plan" else review_row["invocation_id"]
            return (
                [dict(record_id=ref.record_id, event_kind="source_tool_result")]
                if selector_stage is not None and current["invocation_id"] == chosen
                else []
            )

        def read(self, target):
            if target.record_id == row["output_record_id"]:
                return self.output(row)
            if target.record_id == review_row["output_record_id"]:
                return self.output(review_row)
            return self.records[target.record_id]

        def record(self, payload, *, record_id):
            self.records[record_id] = payload

    from podgen.runtime.evaluation import research_selection
    from podgen.runtime.evaluation.planning import clearance_ref

    journal = Journal()
    journal.records[ref.record_id] = event
    journal.records[clearance_ref(run).record_id] = dict(
        run_id=str(run),
        decision="accept",
        candidate_plan_ref=RuntimeRef(
            record_kind="tool_execution", record_id=row["output_record_id"]
        ).model_dump(mode="json"),
        candidate_plan_hash=digest(plan),
        plan_review_ref=RuntimeRef(
            record_kind="tool_execution", record_id=review_row["output_record_id"]
        ).model_dump(mode="json"),
    )
    monkeypatch.setattr(research_selection, "_read_optional", lambda j, r: j.records.get(r.record_id))

    tools = ResearchTools(
        None,
        dict(stage=selector_stage, invocation_id=invocation, run_id=run),
        SourceCorpus(research_materials),
        None,
        lambda: None,
    )
    if selector_stage is not None:
        assert tools.execute(
            "select_research_result",
            dict(result_id="X1", reason="The chosen account requires this report."),
            read_id="E1",
        )["selected"]
    restore(journal, run, research_materials)
    event_reads = journal.event_reads
    assert event_reads > 0
    restore(journal, run, research_materials)
    assert journal.event_reads == event_reads
    payload = request_payload(
        preview_request(run, "draft", _profile("draft"), research_materials, approved_plan=plan)
    )
    if selector_stage is None:
        assert "supplemental_research" not in payload
        assert research_materials.runtime_annex[digest(plan)] == []
        # A saved empty selection must still bind the actual clearance.
        from podgen.domain.errors import ContractError

        journal.records[clearance_ref(run).record_id]["candidate_plan_hash"] = "b" * 64
        with pytest.raises(ContractError, match="research_annex_plan_binding_changed"):
            restore(journal, run, research_materials)
        return
    assert payload["supplemental_research"] == [
        dict(
            result_id="X1",
            title=report.title,
            selection_reason="The chosen account requires this report.",
            text=text,
            reported_source_map={"S1": "S99"},
            kind="research_synthesis_not_source_evidence",
        )
    ]
    assert payload["support"][0]["text"] and "UNSELECTED" not in str(payload)
    declared = {
        tool["name"] for tool in preview_request(run, "plan", _profile("plan"), research_materials)["tools"]
    }
    assert "select_research_result" in declared
    from dataclasses import replace
    from types import SimpleNamespace

    from podgen.writing.planning_versions import planning_snapshot

    fixed = replace(research_materials, fixed=SimpleNamespace(plan=planning_snapshot(plan)))
    fixed_payload = request_payload(preview_request(run, "draft", _profile("draft"), fixed))
    assert fixed_payload["supplemental_research"] == payload["supplemental_research"]
    assert "X1" not in tools.corpus.documents
    changed = dict(plan, listener_question="A different accepted plan")
    other = {}
    attach(other, research_materials, changed)
    assert other == {} and digest(changed) != digest(plan)


def test_supplemental_reports_are_discoverable_without_inlining_synthesis(research_materials):
    text = "New full research synthesis. " * 1000
    research_materials.runtime_admissions.append(
        SourceAdmission(
            "X1",
            "Exact unresolved question?",
            text,
            sha256(text.encode()).hexdigest(),
            RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
            dict(
                kind="research_report",
                reported_source_map=[
                    dict(
                        report_numbers=[7],
                        source_id="S99",
                        reported_url="https://example.org/record",
                        availability="available",
                    )
                ],
            ),
        )
    )
    tools = ResearchTools(
        None,
        dict(stage="plan-review", invocation_id=uuid4(), run_id=uuid4()),
        SourceCorpus(research_materials),
        None,
        lambda: None,
    )
    body = preview_request(uuid4(), "plan", _profile("plan"), research_materials)
    payload = request_payload(body)
    assert payload["research_results"] == dict(tool="list_research_results", offset=0, version=None)
    catalog = tools.execute("list_research_results", dict(offset=0, version=None), read_id="E1")
    assert catalog["reports"][0]["result_id"] == "X1"
    assert catalog["reports"][0]["title"] == "Exact unresolved question?" and text not in str(catalog)
    opened = tools.execute("read_research_result", dict(result_id="X1", start=0, end=None), read_id="E2")
    assert opened["material_passage"]["read_id"] == "R2"
    assert opened["source_mapping"]["tool"] == "list_research_sources"
    mapping = tools.execute(
        "list_research_sources", dict(result_id="X1", offset=0, version=None), read_id="E3"
    )
    assert mapping["sources"][0]["source_id"] == "S99" and mapping["sources"][0]["report_numbers"] == [7]
    assert "X1" not in tools.corpus.documents
