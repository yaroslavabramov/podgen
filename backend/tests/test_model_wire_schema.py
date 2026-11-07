"""Inspect actual compiler requests for unsupported JSON Schema reference siblings."""

from copy import deepcopy
from uuid import uuid4

from podgen.runtime.evaluation.preview import preview_request
from tests.test_craft_reference import reference_resource
from tests.test_v5_role_wire import fixture


def assert_reference_nodes(schema):
    if isinstance(schema, dict):
        if "$ref" in schema:
            assert set(schema) == {"$ref"}, schema
        for value in schema.values():
            assert_reference_nodes(value)
    elif isinstance(schema, list):
        for value in schema:
            assert_reference_nodes(value)


def test_every_actual_role_request_has_only_supported_reference_nodes(tmp_path, monkeypatch):
    reference_resource(tmp_path, monkeypatch)
    materials, _, plan, profile, script, reader, fact, review = fixture()
    values = {
        "plan": {},
        "plan-review": dict(candidate=plan),
        "plan-review-revised": dict(candidate=plan),
        "plan-revise": dict(candidate=plan, plan_review=review),
        "draft": dict(approved_plan=plan),
        "review": dict(approved_plan=plan, draft=script),
        "revise": dict(approved_plan=plan, draft=script, feedback=dict(reader, reading_mode="informed")),
    }
    for index in range(3):
        suffix = "" if index == 0 else f"-{index}"
        values["reader-review" + suffix] = dict(draft=script)
        values["fact-review" + suffix] = dict(draft=script)
        if index:
            values[f"revise-{index}"] = dict(
                approved_plan=plan, draft=script, reader_feedback=reader, fact_feedback=fact
            )
    broken_reader = deepcopy(reader)
    broken_reader["findings"][0]["anchors"][0]["utterance_index"] = 999
    values["anchor-repair"] = dict(draft=script, feedback=broken_reader)
    broken_plan_review = deepcopy(review)
    broken_plan_review["findings"][0]["targets"] = ["/missing/path"]
    values["plan-target-repair"] = dict(candidate=plan, plan_review=broken_plan_review)
    for stage, inputs in values.items():
        body = preview_request(uuid4(), stage, profile, materials, **inputs)
        assert_reference_nodes(body["text"]["format"]["schema"])
