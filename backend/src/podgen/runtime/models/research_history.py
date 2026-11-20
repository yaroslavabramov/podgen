"""Keep receipt references in memory; load full historical results only when requested."""

from collections import defaultdict

from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.history_metadata import from_record, project


class RecordView:
    def __init__(self, history, references):
        self.history, self.references = history, references

    def __len__(self):
        return len(self.references)

    def __iter__(self):
        return (self.history.read(ref) for ref in self.references)

    def __reversed__(self):
        return (self.history.read(ref) for ref in reversed(self.references))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return RecordView(self.history, self.references[index])
        return self.history.read(self.references[index])


class ResearchHistory:
    def __init__(self, journal, root):
        self.journal, self.root = journal, root
        self.references = defaultdict(list)
        self.calls = {}
        self.receipts = set()
        self.call_ids = set()
        self.entries = []
        from podgen.runtime.evaluation.source_tool_recovery import lineage

        self.counts = {}
        self.exact_counts = {}
        self.plans = {}
        self.segments = {}
        self.submitted = set()
        self.responses = {}
        self.web_search_turns = set()
        self.last_count_error = None
        roots = lineage(journal, root)
        loader = getattr(journal, "event_metadata", None)
        metadata = (
            loader(roots)
            if loader is not None
            else (
                from_record(
                    RuntimeRef(record_kind="tool_execution", record_id=event["record_id"]),
                    journal.read(RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])),
                )
                for ancestor in roots
                for event in journal.events(ancestor)
            )
        )
        for entry in metadata:
            if entry.invocation_id != str(root["invocation_id"]) and entry.kind in {
                "source_context_plan",
                "source_context_count",
                "source_context_count_error",
            }:
                if entry.kind == "source_context_count" and entry.data.get("count_identity"):
                    self.exact_counts.setdefault(entry.data["count_identity"], entry)
                continue
            self._index(entry)

    def appended(self, ref, kind, *, data=None, record=None):
        entry = (
            from_record(ref, self.journal.read(ref))
            if data is None
            else project(ref, kind, self.root["invocation_id"], data)
        )
        self._index(entry)

    def _index(self, entry):
        ref, kind, data = entry.reference, entry.kind, entry.data
        self.references[kind].append(ref)
        if kind == "source_web_search":
            self.web_search_turns.add(data["turn_index"])
        elif kind == "source_context_count":
            self.counts.setdefault(data["request_hash"], entry)
            if data.get("count_identity"):
                self.exact_counts.setdefault(data["count_identity"], entry)
        elif kind == "source_context_count_error":
            self.last_count_error = entry
        elif kind == "source_context_plan":
            self.plans.setdefault(data["turn_index"], entry)
        elif kind == "submit_intent" and "source_turn_index" in data:
            self.submitted.add(data["source_turn_index"])
        elif kind == "provider_response" and "source_turn_index" in data:
            self.responses[data["source_turn_index"]] = ref
        elif kind == "source_context_segment":
            self.segments.setdefault(data["next_turn"], entry)
            self.entries.append(("conversation", len(self.references[kind]) - 1))
        if kind in {"source_tool_call", "source_tool_result"}:
            self.calls[kind, data["call_index"]] = ref
            if kind == "source_tool_result":
                self.entries.append(("tool_result", data["call_index"]))
            self.call_ids.add(data["call_id"])
            self.receipts.update(entry.receipt_ids)

    def exact_count(self, identity):
        return self.exact_counts.get(identity)

    def retryable_count_error(self, identity, turn_index):
        entry = self.last_count_error
        if (
            entry is not None
            and entry.data.get("retryable") is True
            and entry.data.get("count_identity") == identity
            and entry.data.get("turn_index") == turn_index
        ):
            return entry.reference
        return None

    def counted(self, request_hash):
        return self.counts.get(request_hash)

    def context_plan(self, turn_index):
        return self.plans.get(turn_index)

    def context_segment(self, next_turn):
        return self.segments.get(next_turn)

    def is_submitted(self, index):
        return index in self.submitted

    def submitted_turns(self):
        return frozenset(self.submitted)

    def provider_response(self, turn_index):
        ref = self.responses.get(turn_index)
        return self.read(ref) if ref is not None else None

    def read(self, ref):
        return dict(self.journal.read(ref), _record_id=ref.record_id)

    def of_kind(self, kind):
        return RecordView(self, self.references[kind])

    def for_call(self, kind, index):
        ref = self.calls.get((kind, index))
        return [self.read(ref)] if ref else []

    def latest(self, kind):
        refs = self.references[kind]
        return self.read(refs[-1]) if refs else None
