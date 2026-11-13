"""The previous linear metadata lookups, for exact-wire and SQL cost comparisons."""

from podgen.runtime.evaluation.history_metadata import from_record
from podgen.runtime.models.research_history import ResearchHistory


class LinearMetadataHistory(ResearchHistory):
    def counted(self, request_hash):
        for reference in self.references["source_context_count"]:
            record = self.read(reference)
            if record["data"]["request_hash"] == request_hash:
                return from_record(reference, record)
        return None

    def context_plan(self, turn_index):
        for reference in self.references["source_context_plan"]:
            record = self.read(reference)
            if record["data"]["turn_index"] == turn_index:
                return from_record(reference, record)
        return None

    def context_segment(self, next_turn):
        for reference in self.references["source_context_segment"]:
            record = self.read(reference)
            if record["data"]["next_turn"] == next_turn:
                return from_record(reference, record)
        return None

    def provider_response(self, turn_index):
        return next(
            (
                record
                for record in reversed(self.of_kind("provider_response"))
                if record["data"].get("source_turn_index") == turn_index
            ),
            None,
        )

    def submitted_turns(self):
        return frozenset(
            record["data"]["source_turn_index"]
            for record in self.of_kind("submit_intent")
            if "source_turn_index" in record["data"]
        )
