"""Durable physical turns; the semantic stage and operational attempt lineage do not change."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.database import one, query


def rows(journal, root):
    with journal.repo.database.transaction() as tx:
        journal.authorize(tx)
        return [
            dict(row)
            for row in query(
                tx,
                "SELECT * FROM evaluation_model_turns WHERE project_id=:p AND root_invocation_id=:i ORDER BY turn_index",
                p=journal.project,
                i=root["invocation_id"],
            ).mappings()
        ]


def events(journal, root, *, kinds=None):
    references = [
        RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        for event in journal.events(root)
        if kinds is None or event["event_kind"] in kinds
    ]
    records = []
    with journal.read_operation():
        for offset in range(0, len(references), 64):
            batch = references[offset : offset + 64]
            records.extend(
                dict(record, _record_id=ref.record_id)
                for ref, record in zip(batch, journal.read_many(batch), strict=True)
            )
    return records


class EventHistory:
    """Invocation-local immutable event cache; each relevant receipt is loaded at most once."""

    KINDS = {
        "submit_intent",
        "provider_response",
        "source_tool_call",
        "source_tool_result",
        "source_tools_final_response",
    }

    def __init__(self, journal, root):
        self.journal = journal
        self.records = events(journal, root, kinds=self.KINDS)
        self.submitted = {
            record["data"]["source_turn_index"]
            for record in self.records
            if record["event_kind"] == "submit_intent" and "source_turn_index" in record["data"]
        }

    def of_kind(self, kind):
        return [record for record in self.records if record["event_kind"] == kind]

    def is_submitted(self, index):
        return index in self.submitted

    def submitted_turns(self):
        return frozenset(self.submitted)

    def for_call(self, kind, index):
        return [row for row in self.of_kind(kind) if row["data"].get("call_index") == index]

    def appended(self, ref, kind, *, data=None, record=None):
        if kind == "submit_intent" and data is not None and "source_turn_index" in data:
            self.submitted.add(data["source_turn_index"])
        if kind in self.KINDS:
            self.records.append(
                dict(record if record is not None else self.journal.read(ref), _record_id=ref.record_id)
            )


class TurnJournal:
    def __init__(self, journal, root, index, *, history=None, ledger=None):
        self.journal, self.root, self.index = journal, root, index
        self.history, self.ledger = history, ledger

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def _row(self, tx):
        value = one(
            tx,
            "SELECT * FROM evaluation_model_turns WHERE project_id=:p AND root_invocation_id=:i AND turn_index=:n",
            p=self.journal.project,
            i=self.root["invocation_id"],
            n=self.index,
        )
        if value is None:
            return None
        row = dict(self.root)
        row.update(dict(value), stage=self.root["stage"], attempt_index=0, output_record_id=None)
        return row

    def prepare(self, body):
        turn, phase, _ = self.stage_prepare(body)
        if phase is not None:
            phase.commit()
        return turn

    def stage_prepare(self, body, **options):
        from .turn_preparation import stage_prepare

        return stage_prepare(self, body, **options)

    def publication(self, invocation):
        from .publication import Publication

        return Publication(self, invocation)

    def transition(self, invocation, status, **fields):
        phase = self.publication(invocation)
        phase.transition(status, **fields)
        return phase.commit()

    def write_transition(
        self,
        tx,
        invocation,
        status,
        *,
        provider_id=None,
        input_tokens=None,
        output_tokens=None,
        output_ref=None,
    ):
        value = one(
            tx,
            """UPDATE evaluation_model_turns SET status=:s,
                provider_response_id=COALESCE(:provider,provider_response_id),
                input_tokens=COALESCE(:input,input_tokens),
                output_tokens=COALESCE(:output,output_tokens)
                WHERE project_id=:p AND root_invocation_id=:i AND turn_index=:n RETURNING *""",
            s=status,
            provider=provider_id,
            input=input_tokens,
            output=output_tokens,
            p=self.journal.project,
            i=self.root["invocation_id"],
            n=self.index,
        )
        row = dict(self.root)
        row.update(dict(value), stage=self.root["stage"], attempt_index=0, output_record_id=None)
        if self.ledger is not None and (
            self.ledger.history.is_submitted(self.index)
            or status in ("submitting", "waiting", "response_completed", "outcome_unknown")
        ):
            usage = self.ledger.projected(row)
            overflow = usage["input"] > 2**63 - 1 or usage["known_output"] > 2**63 - 1
            query(
                tx,
                """UPDATE evaluation_model_invocations
                SET status=:s,input_tokens=:input,output_tokens=:output
                WHERE project_id=:p AND invocation_id=:i""",
                p=self.journal.project,
                i=self.root["invocation_id"],
                s="validation_failed"
                if overflow
                else (
                    status if status in ("failed", "validation_failed", "outcome_unknown") else "tool_running"
                ),
                input=None if overflow else usage["input"],
                output=None if overflow or usage["unknown"] else usage["known_output"],
            )
        return row

    def event_binding(self, invocation, data):
        value = dict(data or {})
        provider = value.pop("provider_response_id", None)
        if provider:
            value["turn_provider_response_id"] = provider
        value.update(
            source_turn_index=self.index,
            source_turn_id=str(invocation["invocation_id"]),
            turn_request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=invocation["request_record_id"]
            ).model_dump(mode="json"),
        )
        return self.root, value

    def event(self, invocation, kind, *, reply=None, data=None):
        phase = self.publication(invocation)
        ref = phase.event(kind, reply=reply, data=data)
        phase.commit()
        return ref

    def last_response(self, invocation):
        if self.history is not None and hasattr(self.history, "provider_response"):
            record = self.history.provider_response(self.index)
            if record is None:
                return None
            ref = RuntimeRef(record_kind="tool_execution", record_id=record["_record_id"])
            return self.journal.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
        records = (
            self.history.of_kind("provider_response")
            if self.history is not None
            else events(self.journal, self.root, kinds={"provider_response"})
        )
        for record in reversed(records):
            if record["data"].get("source_turn_index") == self.index:
                ref = RuntimeRef(record_kind="tool_execution", record_id=record["_record_id"])
                return self.journal.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
        return None


def charged_rows(journal, root, *, history=None, all_rows=None):
    all_rows = rows(journal, root) if all_rows is None else all_rows
    if history is not None and hasattr(history, "submitted_turns"):
        submitted = history.submitted_turns()
    else:
        submitted = {
            record["data"]["source_turn_index"]
            for record in (
                history.of_kind("submit_intent")
                if history is not None
                else events(journal, root, kinds={"submit_intent"})
            )
            if "source_turn_index" in record["data"]
        }
    charged = []
    for row in all_rows:
        if row["turn_index"] not in submitted:
            continue
        if row["status"] == "failed" and row["provider_response_id"] is None:
            from .contracts import RunFailure
            from .credit_recovery import refusal_evidence
            from .source_tool_recovery import TurnEvidence

            view = TurnEvidence(journal, root, row)
            try:
                refusal_evidence(view, view.turn)
            except RunFailure:
                from .schema_refusal import evidence as schema_refusal

                if schema_refusal(view, view.turn) is not None:
                    continue
            else:
                continue
        charged.append(row)
    return charged


def usage(journal, root, output_limit, *, history=None):
    charged = charged_rows(journal, root, history=history)
    return dict(
        input=sum(row["input_tokens"] or 0 for row in charged),
        output=sum(
            row["output_tokens"] if row["output_tokens"] is not None else output_limit for row in charged
        ),
        known_output=sum(row["output_tokens"] or 0 for row in charged),
        unknown=any(row["output_tokens"] is None for row in charged),
    )


def aggregate(journal, root, value, status):
    with journal.repo.database.transaction() as tx:
        journal.authorize(tx)
        query(
            tx,
            "UPDATE evaluation_model_invocations SET status=:s,input_tokens=:input,output_tokens=:output WHERE project_id=:p AND invocation_id=:i",
            s=status,
            input=value["input"],
            output=None if value["unknown"] else value["known_output"],
            p=journal.project,
            i=root["invocation_id"],
        )
