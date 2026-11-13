"""Keep receipt references in memory; load full historical results only when requested."""

from collections import defaultdict

from podgen.domain.contracts.common import RuntimeRef


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

        for ancestor in lineage(journal, root):
            for event in journal.events(ancestor):
                if ancestor["invocation_id"] != root["invocation_id"] and event["event_kind"] in {
                    "source_context_plan",
                    "source_context_count",
                    "source_context_count_error",
                }:
                    continue
                self.appended(
                    RuntimeRef(record_kind="tool_execution", record_id=event["record_id"]),
                    event["event_kind"],
                )

    def appended(self, ref, kind):
        self.references[kind].append(ref)
        if kind == "source_context_segment":
            self.entries.append(("conversation", len(self.references[kind]) - 1))
        if kind in {"source_tool_call", "source_tool_result"}:
            record = self.journal.read(ref)
            data = record["data"]
            self.calls[kind, data["call_index"]] = ref
            if kind == "source_tool_result":
                self.entries.append(("tool_result", data["call_index"]))
            self.call_ids.add(data["call_id"])
            result = data.get("result", {})
            for field in ("passage", "material_passage"):
                if field in result:
                    self.receipts.add(result[field]["read_id"])

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
