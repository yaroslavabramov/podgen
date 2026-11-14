"""One atomic semantic result and completion, recoverable without repeating inference."""

from uuid import UUID

from podgen.persistence.private_journal.contracts import SemanticResult


def complete(journal, payload, reference):
    invocation = journal.invocation(UUID(payload["run_id"]), payload["stage"])
    phase = journal.publication(invocation)
    phase.semantic_result = SemanticResult(reference, payload, ())
    phase.transition("completed", output_ref=reference)
    return phase.commit()
