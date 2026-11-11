"""Native address repair over an in-memory archive of the rejected reader response."""

import json
from copy import deepcopy
from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.attempts import attempt_identity, validate_preparation
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.review_repair import apply_anchor_repair, prepare_anchor_repair
from podgen.runtime.models.contracts import WireReply
from podgen.writing.projections import CONTRACT, indexed_draft
from podgen.writing.reader_schema import COMPACT_VERSION
from podgen.writing.revision import verification
from tests.source_tool_runtime_support import MemoryJournal
from tests.test_required_recheck_result import review


def ref(identity):
    return RuntimeRef(record_kind="tool_execution", record_id=identity).model_dump(mode="json")


class RepairJournal(MemoryJournal):
    def prepare(self, run_id, stage, body, context, **kwargs):
        index, kind, operation = (kwargs[key] for key in ("attempt_index", "attempt_kind", "operation_kind"))
        parent = validate_preparation(
            self.attempts(run_id, stage), stage, index, kind, operation, kwargs["parent_invocation_id"]
        )
        row = dict(
            self.root,
            invocation_id=attempt_identity(run_id, stage, index, kind, parent),
            request_record_id=self.store_request(body),
            status="prepared",
            output_record_id=None,
            provider_response_id=None,
            parent_attempt_index=parent["attempt_index"],
        )
        row.update({key: value for key, value in kwargs.items() if key != "authorization"})
        self.records[row["request_record_id"]].update(
            contract="model-request-v1",
            invocation_id=str(row["invocation_id"]),
            run_id=str(run_id),
            stage=stage,
            context=deepcopy(context),
            attempt_kind=kind,
            operation_kind=operation,
            parent_request_ref=ref(parent["request_record_id"]),
        )
        self.roots[row["invocation_id"]] = row
        return row

    def output(self, row):
        return self.records[row["output_record_id"]]


def repaired_case(version, retry=False):
    script, rejected = review("solved_differently")
    rejected["findings"][0]["anchors"][0]["utterance_index"] = 99
    comparison = dict(
        assignments=[dict(id="R1")],
        previous_script_in_order=[dict(copy_current=dict(first=0, last=len(script["utterances"]) - 1))],
    )
    journal = RepairJournal(
        dict(
            input_contract=CONTRACT,
            reader_review_schema_version=COMPACT_VERSION,
            **{verification.KEY: version},
        ),
        {},
    )
    root = journal.root
    root.update(
        stage="reader-review-1",
        parent_attempt_index=None,
        status="validation_failed",
        provider_response_id="resp_reader",
        input_tokens=100,
    )
    script_ref = ref(uuid4())
    body = dict(
        model="test",
        instructions="Frozen reader instructions.",
        input=[
            dict(
                role="user",
                content=[
                    dict(
                        type="input_text",
                        text=json.dumps(dict(script=indexed_draft(script), revision_check=comparison)),
                    )
                ],
            )
        ],
        text=dict(
            format=dict(
                type="json_schema",
                name="reader_review",
                schema=verification.schema(version, reader_schema_version=COMPACT_VERSION),
            )
        ),
        metadata=dict(
            podgen_invocation_id=str(root["invocation_id"]),
            podgen_run_id=str(root["run_id"]),
            podgen_stage=root["stage"],
        ),
    )
    root["request_record_id"] = journal.store_request(body)
    context = dict(
        role_input_contract=CONTRACT,
        script_ref=script_ref,
        script_hash=digest(script),
        revision_verification=dict(contract=version, comparison_hash=digest(comparison)),
    )
    journal.records[root["request_record_id"]].update(
        contract="model-request-v1",
        run_id=str(root["run_id"]),
        stage=root["stage"],
        invocation_id=str(root["invocation_id"]),
        operation_kind="role",
        attempt_kind="role",
        parent_request_ref=None,
        context=context,
    )
    response = dict(
        id="resp_reader",
        status="completed",
        model=body["model"],
        metadata=body["metadata"],
        output=[
            dict(
                type="message",
                role="assistant",
                content=[dict(type="output_text", text=json.dumps(rejected))],
            )
        ],
    )
    journal.event(root, "provider_response", reply=WireReply(json.dumps(response).encode()))
    leaf = prepare_anchor_repair(journal, root, draft=script, draft_ref=script_ref, guard=lambda: None)
    if retry:
        leaf["status"] = "outcome_unknown"
        next_body = deepcopy(journal.request(leaf))
        next_id = attempt_identity(root["run_id"], root["stage"], 2, "technical_retry", leaf)
        next_body["metadata"].update(
            podgen_invocation_id=str(next_id),
            podgen_attempt_index="2",
            podgen_parent_invocation_id=str(leaf["invocation_id"]),
        )
        leaf = journal.prepare(
            root["run_id"],
            root["stage"],
            next_body,
            deepcopy(journal.records[leaf["request_record_id"]]["context"]),
            attempt_index=2,
            attempt_kind="technical_retry",
            operation_kind="anchor_repair",
            parent_invocation_id=leaf["invocation_id"],
        )
    offered = request_payload(journal.request(leaf))
    choices = dict(
        choices=[
            dict(anchor_id=row["anchor_id"], selector_id=row["selector_ids"][0])
            for row in offered["invalid_anchors"]
        ]
    )
    fixed, _ = apply_anchor_repair(journal, leaf, json.dumps(choices), draft=script)
    leaf.update(status="completed", output_record_id=uuid4())
    journal.records[leaf["output_record_id"]] = dict(
        request_ref=ref(leaf["request_record_id"]), output=fixed, repair_choices=choices
    )
    return journal, root, leaf, script, comparison, fixed
