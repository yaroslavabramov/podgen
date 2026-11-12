from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, RuntimeRef


class AssemblyConfig(Contract):
    contract: Literal["audio-evaluation-assembly/1"] = "audio-evaluation-assembly/1"
    assembly_id: UUID
    comparison_run_id: UUID
    part_result_refs: list[RuntimeRef] = Field(min_length=1)
    preferred_lufs: float = Field(default=-20, ge=-40, le=-14)
    true_peak_dbtp: float = Field(default=-1, ge=-6, le=-1)
    headroom_db: float = Field(default=0.3, ge=0.1, le=3)
    join_context_seconds: float = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def unique_parts(self):
        if len(self.part_result_refs) != len(set(self.part_result_refs)):
            raise ValueError("assembly_duplicate_part")
        return self


def prefix(assembly_id):
    return "assembly:" + str(assembly_id)
