"""Exact terminal-review requests, sharing the ordinary bound research operation."""

from copy import deepcopy
from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.research.materials.orders import VERSION as ORDER_VERSION
from podgen.research.materials.orders import policy
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.research_binding import bind, request_arguments

VERSION = "episode-material-question/2"


def collect(journal, outcome):
    references = [RuntimeRef.model_validate(ref) for ref in outcome["reading_refs"].values()]
    records = journal.read_many(references)
    questions = []
    for reference, record in zip(references, records, strict=True):
        bound = (
            record["stage"].startswith("fact-review")
            and policy(journal.manifest(UUID(record["run_id"]))) == ORDER_VERSION
        )
        for request in record["output"].get("research_requests", []):
            if bound:
                if request.get("blocking", True) is False:
                    continue
                questions.append(
                    dict(
                        contract=VERSION,
                        request=deepcopy(request),
                        reading_ref=reference.model_dump(mode="json"),
                    )
                )
            else:
                questions.append(request)
    if not questions:
        raise ContractError("episode_material_request_has_no_concrete_question")
    return questions, outcome["script_ref"]


def resolve(parent, writing_runner, progress, question, guard):
    from .writing import saved_command

    journal = EvaluationJournal(parent.repo, parent.project)
    ref = RuntimeRef.model_validate(question["reading_ref"])
    record = journal.read(ref)
    run_id = UUID(progress["writing_run_id"])
    if (
        record["run_id"] != str(run_id)
        or not record["stage"].startswith("fact-review")
        or policy(journal.manifest(run_id)) != ORDER_VERSION
        or question["request"] not in record["output"].get("research_requests", [])
        or record["script_ref"] != progress["continuation_script_ref"]
    ):
        raise ContractError("episode_material_question_binding_mismatch")
    root = journal.invocation(run_id, record["stage"])
    if (
        root is None
        or root["status"] != "completed"
        or str(root["output_record_id"]) != str(ref.record_id)
        or str(root["invocation_id"]) != record["invocation_id"]
    ):
        raise ContractError("episode_material_question_caller_mismatch")
    arguments = bind(
        journal, root, request_arguments(question["request"], anchored=True), request_policy=ORDER_VERSION
    )
    if arguments["script_context"]["script_hash"] != record["script_hash"]:
        raise ContractError("episode_material_question_script_mismatch")
    command = saved_command(parent.repo, parent.project, run_id)
    resolver = writing_runner.material_resolver(command, writing_runner._materials(command))
    operation = uuid5(parent.run_id, "terminal-material/2:" + digest(question))
    return resolver.resolve(
        "request_research", arguments, operation_id=operation, run_id=run_id, guard=guard
    ).result
