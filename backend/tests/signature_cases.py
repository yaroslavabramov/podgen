"""Frozen admitted primary bytes used by the Snow and rain fact requests."""

import hashlib
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.materials import AuthorDocument

from tests.source_excerpt_cases import data_and_profile

ROOT = Path(__file__).resolve().parents[2]


def materials_for(case):
    base = (
        ROOT
        / "evals/editorial-inputs"
        / ("current/snow-pump" if case == "snow-pump" else "experiments/rain-soil")
    )
    packet = EditorialInput.model_validate_json((base / "shared.json").read_bytes())
    docs = []
    for source in packet.documents:
        if source.purpose != "primary":
            continue
        directory = ROOT / "evals/cases/development/wp02-batch-01" if case == "snow-pump" else base
        raw = (directory / source.source_path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source.sha256
        docs.append(
            AuthorDocument(
                source.source_path,
                raw.decode(),
                source.sha256,
                RuntimeRef(record_kind="user_input", record_id=uuid4()),
            )
        )
    original = data_and_profile()
    return EditorialMaterials(packet, original[0].shared_ref, tuple(docs)), original[-1]
