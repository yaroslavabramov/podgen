from copy import deepcopy

import pytest

from podgen.writing.revision.contracts import SequenceRevision, StructuralRevision, TargetedRevision
from podgen.writing.roles import WriteOutput
from podgen.writing.speaker_schema import bind_script_speakers


@pytest.mark.parametrize("model", [WriteOutput, TargetedRevision, SequenceRevision, StructuralRevision])
@pytest.mark.parametrize("labels", [("РАССКАЗЧИК",), ("Нина", "Илья")])
def test_wire_offers_only_exact_assigned_speakers_without_modifying_shared_schema(model, labels):
    schema = model.model_json_schema()
    original = deepcopy(schema)
    bound = bind_script_speakers(schema, labels)
    assert schema == original
    assert bound["$defs"]["WriteUtterance"]["properties"]["speaker"]["enum"] == list(labels)
    assert (
        bound["$defs"]["WriteUtterance"]["properties"]["text"]
        == original["$defs"]["WriteUtterance"]["properties"]["text"]
    )
    assert bind_script_speakers(schema, ()) == original
