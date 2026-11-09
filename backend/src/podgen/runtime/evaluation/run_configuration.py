"""Resolve new production defaults at admission, keeping omitted historical DTO fields readable."""

from podgen.writing.behavior import PRODUCTION

from .contracts import RunLimits
from .wire_budget import PRODUCTION_RUN_LIMITS


def production_command(command, *, frozen_manifest=None):
    if frozen_manifest is not None and frozen_manifest.get("behavior_profile") != PRODUCTION:
        return command
    if command.input_contract != "editorial-role-packets/1" or command.behavior_profile not in (
        None,
        PRODUCTION,
    ):
        return command
    updates = dict(behavior_profile=PRODUCTION)
    if "review_anchor_policy" not in command.model_fields_set:
        updates["review_anchor_policy"] = (
            frozen_manifest["command"].get("review_anchor_policy", "strict")
            if frozen_manifest is not None
            else "bounded-selection-v1"
        )
    if "limits" not in command.model_fields_set:
        updates["limits"] = RunLimits(**PRODUCTION_RUN_LIMITS)
    resolved = command.model_copy(update=updates)
    return type(command).model_validate(resolved.model_dump(mode="python"))
