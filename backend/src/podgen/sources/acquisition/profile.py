from podgen.domain.errors import ContractError

from .contracts import VERSION, AcquisitionProfile


def load_profile(repo, project_id, profile_ref):
    if profile_ref.kind != "ProcessingProfile":
        raise ContractError("source_acquisition_requires_extraction_profile")
    payload = repo.get(project_id, profile_ref).payload
    if payload["profile_kind"] != "extraction":
        raise ContractError("source_acquisition_requires_extraction_profile")
    if payload["implementation"] != VERSION or payload["version"] != "1":
        raise ContractError("source_acquisition_unsupported_profile")
    return AcquisitionProfile.model_validate(payload["configuration"])
