"""Select only explicitly pinned audit schemas; historical contracts remain independent."""

from podgen.domain.errors import ContractError

from . import FACT_SCHEMA, KEY, READER_SCHEMA, RECHECK_SCHEMA, VERSION
from .schemas import AuditedFact, AuditedReader


def from_manifest(manifest):
    from .consistency import from_manifest as consistency_from_manifest

    consistency_from_manifest(manifest)
    value = manifest.get(KEY)
    if value is not None and (
        value != VERSION
        or manifest.get("reader_review_schema_version") != READER_SCHEMA
        or manifest.get("fact_review_schema_version") != FACT_SCHEMA
        or manifest.get("revision_verification_contract") != RECHECK_SCHEMA
    ):
        raise ContractError("reading_audit_contract_changed")
    if value is None and (
        manifest.get("reader_review_schema_version") == READER_SCHEMA
        or manifest.get("fact_review_schema_version") == FACT_SCHEMA
        or manifest.get("revision_verification_contract") == RECHECK_SCHEMA
    ):
        raise ContractError("reading_audit_pin_required")
    return value


def model(stage):
    if stage == "fact-review" or stage.startswith("fact-review-"):
        return AuditedFact
    if stage == "reader-review" or stage.startswith("reader-review-"):
        return AuditedReader
    raise ContractError("unknown_reading_audit_role")


def schema(stage, *, consistency_contract=None):
    result = model(stage).model_json_schema()
    from .descriptions import describe_fact, describe_reader

    (describe_fact if model(stage) is AuditedFact else describe_reader)(result)
    from .consistency import describe

    describe(result, consistency_contract)
    return result
