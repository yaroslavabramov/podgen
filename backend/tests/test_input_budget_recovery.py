"""One serial SQL/gateway check: exact unsent retry, separate local limits, no duplicate submits."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits
from podgen.runtime.evaluation.input_budget_evidence import unsent_evidence
from podgen.runtime.evaluation.input_budget_recovery import effective_limits
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.recovery import authorize_recovery
from podgen.runtime.evaluation.usage import prior_attempt_usage
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from tests.test_writing_evaluation import setup_writer as setup_writer


class CountedProvider:
    def __init__(self):
        self.counts, self.submits = [], []

    def count(self, body):
        self.counts.append(deepcopy(body))
        return WireReply(b'{"input_tokens":35000}')

    def submit(self, body, *, client_request_id):
        self.submits.append(deepcopy(body))
        return WireReply(
            json.dumps(
                dict(
                    id="resp_budget",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(type="message", role="assistant", content=[dict(type="output_text", text="{}")])
                    ],
                    usage=dict(input_tokens=35000, output_tokens=5),
                )
            ).encode()
        )


def test_exact_unsent_budget_retry_preserves_history_counts_tokens_and_keeps_all_other_guards(
    repo, setup_writer
):
    original_command, _, _ = setup_writer
    profile = SimpleNamespace(
        provider="openai",
        api_family="responses",
        adapter_version="openai-responses-v1",
        model_revision_or_alias="synthetic-budget",
        output_limit=24576,
        context_limit=200000,
        timeout_policy=dict(poll_deadline_seconds=10),
        generation_parameters=dict(store=False),
    )

    def guard():
        return None

    def blocked():
        command = original_command.model_copy(
            update=dict(
                run_id=uuid4(),
                input_contract="editorial-role-packets/1",
                review_mode="independent-readings/1",
                max_script_revisions=2,
            )
        )
        journal = EvaluationJournal(repo, command.project_id)
        journal.create_run(
            command,
            dict(
                input_contract=command.input_contract, role_projection_version="editorial-role-projections/9"
            ),
        )
        body = dict(
            model="synthetic-budget",
            instructions="Preserve exactly.",
            input=[dict(role="user", content=[dict(type="input_text", text="Ж" * 67000)])],
            text=dict(format=dict(schema=dict(type="object"))),
            metadata=dict(
                podgen_run_id=str(command.run_id),
                podgen_stage="revise-1",
                podgen_invocation_id=str(uuid5(command.run_id, "invocation:revise-1")),
            ),
        )
        row = journal.prepare(
            command.run_id,
            "revise-1",
            body,
            dict(
                role_input_contract=command.input_contract,
                role_projection_version="editorial-role-projections/9",
                immutable_source="same script, plan, reviews and profile",
            ),
        )
        provider = CountedProvider()
        constructed = []
        gateway = ModelGateway(journal, lambda _: constructed.append(True) or provider)
        with pytest.raises(RunFailure, match="role_visible_byte_budget_exceeded"):
            gateway.invoke(row, profile, limits=RunLimits(), totals=dict(input=0, output=0), guard=guard)
        assert not constructed and not provider.counts and not provider.submits
        return command, journal, journal.invocation(command.run_id, "revise-1"), gateway, provider

    command, journal, original, gateway, provider = blocked()
    saved, events, manifest = (
        journal.request(original),
        journal.events(original),
        journal.manifest(command.run_id),
    )
    with pytest.raises(RunFailure):
        gateway.invoke(original, profile, limits=RunLimits(), totals=dict(input=0, output=0), guard=guard)
    retry = authorize_recovery(
        journal, command, budget_target=original["invocation_id"], max_visible_bytes=262144, guard=guard
    )
    with pytest.raises(RunFailure, match="role_input_token_budget_exceeded"):
        gateway.invoke(retry, profile, limits=RunLimits(), totals=dict(input=2000, output=100), guard=guard)
    assert len(provider.counts) == 1 and not provider.submits
    retry = journal.invocation(command.run_id, "revise-1")
    assert retry["input_tokens"] == 35000
    next_retry = authorize_recovery(
        journal, command, budget_target=retry["invocation_id"], max_input_tokens=65536, guard=guard
    )
    assert effective_limits(journal, next_retry) == dict(visible_bytes=262144, input_tokens=65536)
    assert prior_attempt_usage(journal, next_retry, profile) == dict(input=0, output=0)
    assert (
        gateway.invoke(
            next_retry, profile, limits=RunLimits(), totals=dict(input=2000, output=100), guard=guard
        )
        == "{}"
    )
    assert len(provider.counts) == 2 and len(provider.submits) == 1
    assert provider.counts[0] == provider.counts[1]
    record = journal.record(dict(output={}))
    complete = journal.transition(
        journal.invocation(command.run_id, "revise-1"), "completed", output_ref=record
    )
    assert (
        gateway.invoke(complete, profile, limits=RunLimits(), totals=dict(input=0, output=0), guard=guard)
        == "{}"
    )
    assert (
        authorize_recovery(
            journal, command, budget_target=retry["invocation_id"], max_input_tokens=65536, guard=guard
        )["invocation_id"]
        == complete["invocation_id"]
    )
    assert len(provider.submits) == 1
    assert (
        journal.request(original) == saved
        and journal.events(original) == events
        and journal.manifest(command.run_id) == manifest
    )
    latest = journal.request(complete)
    assert {k: v for k, v in saved.items() if k != "metadata"} == {
        k: v for k, v in latest.items() if k != "metadata"
    }
    with pytest.raises(Conflict, match="input_budget_allowance_changed"):
        authorize_recovery(
            journal, command, budget_target=retry["invocation_id"], max_input_tokens=70000, guard=guard
        )

    for code, context_limit, limits in [
        ("context_capacity_exceeded", 40000, RunLimits()),
        ("run_token_budget_exceeded", 200000, RunLimits(max_total_input_tokens=36000)),
        ("run_token_budget_exceeded", 200000, RunLimits(max_total_output_tokens=24576)),
    ]:
        command, journal, original, gateway, provider = blocked()
        retry = authorize_recovery(
            journal,
            command,
            budget_target=original["invocation_id"],
            max_visible_bytes=262144,
            max_input_tokens=65536,
            guard=guard,
        )
        local_profile = SimpleNamespace(**(vars(profile) | dict(context_limit=context_limit)))
        with pytest.raises(RunFailure, match=code):
            gateway.invoke(
                retry, local_profile, limits=limits, totals=dict(input=2000, output=100), guard=guard
            )
        assert len(provider.counts) == 1 and not provider.submits
        with pytest.raises(RunFailure, match="input_budget_retry_not_eligible"):
            authorize_recovery(
                journal, command, budget_target=retry["invocation_id"], max_input_tokens=70000, guard=guard
            )

    command, journal, original, _, provider = blocked()
    # A stale validation-failed projection cannot authorize a retry after a submit intent.
    phase = journal.publication(original)
    phase.event(
        "submit_intent",
        data=dict(client_request_id=str(original["invocation_id"]), output_reserved=profile.output_limit),
    )
    phase.transition("submitting", input_tokens=35000)
    phase.commit()
    with pytest.raises(RunFailure, match="input_budget_retry_not_eligible"):
        unsent_evidence(journal, original)
    with pytest.raises(RunFailure, match="input_budget_retry_not_eligible"):
        authorize_recovery(
            journal,
            command,
            budget_target=original["invocation_id"],
            max_visible_bytes=262144,
            max_input_tokens=65536,
            guard=guard,
        )
    assert len(journal.attempts(command.run_id, "revise-1")) == 1 and not provider.submits
