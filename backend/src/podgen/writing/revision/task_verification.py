"""Source-isolated recheck context from the actual compact writer assignment."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .verification import previous_utterances

DECISION_FIELDS = ("recommendation", "listener_answer", "strengths", "important_limits", "revision_scope")
TASK_FIELDS = ("origin", "anchors", "problem", "action")
PREFIXES = {"reader": "R", "fact": "F"}


def comparison(previous, current, writer_input, reader_decision):
    """Keep exact task prose and permissions; source datasets belong to the fact reader."""
    assignments, counts = [], {"reader": 0, "fact": 0}
    for task in writer_input["revision_tasks"]:
        origin = task["origin"]
        if origin not in counts:
            raise ContractError("revision_verification_task_origin_invalid")
        counts[origin] += 1
        row = {key: deepcopy(task[key]) for key in TASK_FIELDS}
        row["id"] = f"{PREFIXES[origin]}{counts[origin]}"
        if "preserve" in task:
            row["preserve"] = deepcopy(task["preserve"])
        assignments.append(row)
    return dict(
        previous_script_in_order=previous_utterances(previous, current),
        assignments=assignments,
        revision_operation=deepcopy(writer_input["revision_operation"]),
        previous_reader_decision={key: deepcopy(reader_decision[key]) for key in DECISION_FIELDS},
    )
