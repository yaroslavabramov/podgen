from typing import Literal

from podgen.domain.contracts.common import Contract, RuntimeRef


class AudioSubject(Contract):
    kind: Literal["comparison_result", "assembly"]
    ref: RuntimeRef


class SynthesisPart(Contract):
    kind: Literal["comparison_result", "repair_part"]
    ref: RuntimeRef
