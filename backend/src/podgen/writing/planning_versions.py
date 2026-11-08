"""Explicit model-facing planning versions and shape-based lossless snapshot validation."""

from .planning_roles import PlanningOutput
from .projection_versions import V9, validate_version


def planning_model(projection_version, *, legacy=False):
    validate_version(projection_version)
    if projection_version == V9 and not legacy:
        from .planning_v9 import PlanningOutputV9

        return PlanningOutputV9
    return PlanningOutput


def snapshot_model(value):
    plan = value.get("plan")
    if plan and "last_moment" in plan["ending"]:
        from .planning_v9 import PlanningOutputV9

        return PlanningOutputV9
    return PlanningOutput


def planning_snapshot(value):
    return snapshot_model(value).model_validate(value)
