"""Pin delivery of existing editorial material options, separately from timing calculations."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "plan_selection_contract"
VERSION = "plan-material-selection/1"


def for_projection(version):
    return VERSION if version == V9 else None


def validate(version, projection):
    if version is not None and (version != VERSION or projection != V9):
        raise ContractError("unsupported_plan_selection_contract")
    return version


def from_manifest(manifest):
    return validate(manifest.get(KEY), manifest_version(manifest))


def attach(view, plan, *, version, projection):
    validate(version, projection)
    if version is not None:
        options = plan["plan"]["duration_strategy"]["optional_material"]
        if options:
            view["material_options"] = dict(
                guidance=(
                    "Conditional extensions or cuts from the plan. Use them when adjusting the "
                    "episode's scope; listing them does not mean every item should be spoken."
                ),
                choices=deepcopy(options),
            )
