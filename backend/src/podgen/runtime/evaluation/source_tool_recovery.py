"""Target a proven unsent physical turn while retaining the paid conversation and read receipts."""

from copy import deepcopy

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from . import model_turns
from .contracts import RunFailure
from .source_tool_contract import applies, from_manifest


class TurnEvidence:
    """Present one physical turn to existing strict evidence validators, preserving archive references."""

    def __init__(self, journal, root, turn):
        self.journal, self.root = journal, root
        self.turn = dict(root, **turn)
        self.turn.update(attempt_index=0, output_record_id=None)
        self.turn["root_invocation_id"] = root["invocation_id"]
        self._records = {}

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def events(self, row):
        with self.journal.read_operation():
            events = self.journal.events_for_turn(self.root, self.turn["turn_index"])
            return self._selected_events(events)

    def _selected_events(self, events):
        result = []
        for event in events:
            ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
            record = deepcopy(self.journal.read(ref))
            if record["data"].get("source_turn_index") != self.turn["turn_index"]:
                raise Conflict("source_tool_turn_evidence_mismatch")
            if (
                record["invocation_id"] != str(self.root["invocation_id"])
                or record["request_ref"]["record_id"] != str(self.root["request_record_id"])
                or record["data"].get("source_turn_id") != str(self.turn["invocation_id"])
                or record["data"].get("turn_request_ref")
                != RuntimeRef(
                    record_kind="tool_execution", record_id=self.turn["request_record_id"]
                ).model_dump(mode="json")
            ):
                raise Conflict("source_tool_turn_evidence_mismatch")
            record["invocation_id"] = str(self.turn["invocation_id"])
            record["request_ref"] = record["data"]["turn_request_ref"]
            self._records[ref.record_id] = record
            result.append(event)
        return result

    def read(self, ref):
        return (
            deepcopy(self._records[ref.record_id])
            if ref.record_id in self._records
            else self.journal.read(ref)
        )


def evidence_target(journal, root):
    if root.get("root_invocation_id") is not None or root["operation_kind"] != "role":
        return None
    if not applies(root["stage"], from_manifest(journal.manifest(root["run_id"]))):
        return None
    turns = model_turns.rows(journal, root)
    if not turns or root.get("output_record_id") is not None:
        raise RunFailure(
            "source_tool_retry_not_eligible", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    turn = turns[-1]
    if turn["status"] != root["status"] or any(t["status"] != "response_completed" for t in turns[:-1]):
        raise RunFailure(
            "source_tool_retry_not_eligible", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    view = TurnEvidence(journal, root, turn)
    return view, view.turn


def retry_request(journal, root):
    target = evidence_target(journal, root)
    return journal.request(target[1] if target else root)


def resume_authorization(journal, root):
    target = evidence_target(journal, root)
    if target is None:
        return {}
    turn = target[1]
    return dict(
        source_tool_resume=dict(
            source_root_invocation_id=str(root["invocation_id"]),
            turn_invocation_id=str(turn["invocation_id"]),
            turn_index=turn["turn_index"],
        )
    )


def lineage(journal, root):
    """Operational parents supply receipts only; independent editorial repairs use their own origin."""
    result, seen = [root], {root["invocation_id"]}
    while root["attempt_kind"] in (
        "credit_retry",
        "input_budget_retry",
        "total_input_retry",
        "technical_retry",
    ):
        root = next(
            (
                row
                for row in journal.attempts(root["run_id"], root["stage"])
                if row["invocation_id"] == root["parent_invocation_id"]
            ),
            None,
        )
        if root is None or root["invocation_id"] in seen:
            raise Conflict("source_tool_resume_origin_missing")
        seen.add(root["invocation_id"])
        result.append(root)
    return list(reversed(result))


def continuation(journal, root, *, receipt_metadata=None):
    """Validate the explicit authorization and exact failed-turn request before any network action."""
    ancestors = lineage(journal, root)
    for parent, child in zip(ancestors, ancestors[1:], strict=False):
        record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=child["request_record_id"]))
        auth = record["authorization"]
        expected = resume_authorization(journal, parent)["source_tool_resume"]
        if (
            auth.get("source_tool_resume") != expected
            or auth.get("actor_id") != str(journal.repo.actor_id)
            or auth.get("target_invocation_id") != str(parent["invocation_id"])
            or auth.get("maximum_new_attempts") != 1
        ):
            raise Conflict("source_tool_resume_authorization_mismatch")
        if child["attempt_kind"] == "credit_retry":
            from .credit_recovery import refusal_evidence

            receipt = refusal_evidence(journal, parent)
            if auth.get("kind") != "credit-refusal-retry-v1" or auth.get(
                "refusal_event_ref"
            ) != receipt.model_dump(mode="json"):
                raise Conflict("source_tool_resume_authorization_mismatch")
        elif child["attempt_kind"] == "technical_retry":
            from .technical_recovery import validate_retry

            validate_retry(journal, child, parent)
        elif child["attempt_kind"] == "total_input_retry":
            from .total_input_recovery import effective_ceiling

            effective_ceiling(journal, child["run_id"])
        else:
            from .input_budget_recovery import effective_limits

            effective_limits(journal, child)
        body, original = journal.request(child), retry_request(journal, parent)
        if {k: v for k, v in body.items() if k != "metadata"} != {
            k: v for k, v in original.items() if k != "metadata"
        }:
            raise Conflict("source_tool_resume_request_changed")
    if len(ancestors) == 1:
        return 0, []
    auth = journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]))[
        "authorization"
    ]
    if receipt_metadata is None:
        records = [
            record
            for ancestor in ancestors[:-1]
            for record in model_turns.events(journal, ancestor, kinds={"source_tool_result"})
        ]
    else:
        owners = {str(ancestor["invocation_id"]) for ancestor in ancestors[:-1]}
        records = [
            {"data": entry.data}
            for entry in receipt_metadata
            if entry.invocation_id in owners and entry.kind == "source_tool_result"
        ]
    records.sort(key=lambda record: record["data"]["call_index"])
    if [record["data"]["call_index"] for record in records] != list(range(len(records))):
        raise Conflict("source_tool_resume_receipts_changed")
    return auth["source_tool_resume"]["turn_index"], records


def turn_input_policy(journal, root):
    """A local allowance covers one exact turn, including credit retries of that same request."""
    from .input_budget_recovery import DEFAULTS, effective_limits

    manifest = journal.manifest(root["run_id"])
    limits = effective_limits(journal, root, manifest=manifest)
    policy = {None: manifest.get("role_input_budget", DEFAULTS).copy()}
    for ancestor in reversed(lineage(journal, root)):
        if ancestor["attempt_kind"] == "input_budget_retry":
            record = journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=ancestor["request_record_id"])
            )
            policy[record["authorization"]["source_tool_resume"]["turn_index"]] = limits
            break
    return policy


def turn_input_limits(journal, root, turn_index):
    policy = turn_input_policy(journal, root)
    return policy.get(turn_index, policy[None])
