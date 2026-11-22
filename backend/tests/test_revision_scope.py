"""Explicit repair scale, anchored reasons and historical schema identities."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
from podgen.writing.projections import actionable_feedback
from podgen.writing.reader_feedback import ReaderFeedback
from podgen.writing.reader_schema import V1, VERSION
from podgen.writing.roles import WriteOutput
from pydantic import ValidationError

from tests.schema_cases import wire_hash
from tests.test_v5_role_wire import fixture


def feedback(scale="whole_episode"):
    *_, reader, _, _ = fixture()
    reader["revision_scope"] = dict(
        scale=scale,
        reason="Изменить порядок раскрытия: ответ назван раньше обнаружения улики.\n" * 50,
        anchors=deepcopy(reader["findings"][0]["anchors"]),
    )
    return reader


def test_v1_schema_stays_byte_identical_and_v2_alone_requires_the_scope():
    expected = json.loads((Path(__file__).parent / "fixtures/reader-schema-v1.hashes.json").read_text())
    for contract in ("editorial-role-packets/1", "legacy-author-materials/1"):
        for stage in ("reader-review", "reader-review-1", "reader-review-2"):
            old = response_schema(stage, contract, reader_schema_version=V1)
            assert wire_hash(old) == expected["sha256"]
            assert "revision_scope" not in old["properties"]
            new = response_schema(stage, contract, reader_schema_version=VERSION)
            assert "revision_scope" in new["required"]


@pytest.mark.parametrize("scale", ["local", "sequence", "whole_episode"])
def test_the_same_text_can_receive_each_explicit_scale_without_count_based_inference(scale):
    review = feedback(scale)
    script = fixture()[4]
    parsed = ReaderFeedback.model_validate(review)
    parsed.validate_against(WriteOutput.model_validate(script))
    projected = actionable_feedback(parsed.model_dump(mode="json"))
    assert projected["revision_scope"] == review["revision_scope"]
    assert projected["findings"] == review["findings"]
    assert projected["revision_scope"] is not review["revision_scope"]


def test_keep_can_preserve_a_good_text_without_a_required_repair():
    review = feedback("none")
    review.update(recommendation="keep", findings=[])
    review["revision_scope"].update(reason="Целый рассказ работает; обязательной правки нет.", anchors=[])
    assert ReaderFeedback.model_validate(review).revision_scope.scale == "none"
    for change, error in (
        (dict(recommendation="revise"), "revise_requires_revision_scope"),
        (
            dict(
                revision_scope=dict(
                    review["revision_scope"], scale="local", anchors=feedback()["revision_scope"]["anchors"]
                )
            ),
            "keep_cannot_require_revision",
        ),
    ):
        with pytest.raises(ValidationError, match=error):
            ReaderFeedback.model_validate(dict(review, **change))


def test_scope_addresses_use_the_normal_exact_quote_validation_and_repair():
    review = feedback()
    review["revision_scope"]["anchors"][0]["utterance_index"] = 999
    parsed, script = ReaderFeedback.model_validate(review), WriteOutput.model_validate(fixture()[4])
    with pytest.raises(ValueError, match="feedback_utterance_index_out_of_range"):
        parsed.validate_against(script)
    plan = AnchorRepairPlan.build(script, parsed)
    assert [row.anchor_id for row in plan.broken] == ["revision_scope/0/anchors/0"]
    repaired = plan.apply(
        AnchorChoices.model_validate(
            dict(choices=[dict(anchor_id=plan.broken[0].anchor_id, selector_id=plan.passages[0].selector_id)])
        )
    )
    assert repaired.revision_scope.reason == parsed.revision_scope.reason
    assert repaired.revision_scope.scale == parsed.revision_scope.scale
    assert repaired.revision_scope.anchors[0].utterance_index == 0
    with pytest.raises(ValidationError, match="revision_scope_requires_anchor"):
        ReaderFeedback.model_validate(dict(review, revision_scope=dict(review["revision_scope"], anchors=[])))
