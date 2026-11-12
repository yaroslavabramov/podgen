import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, RuntimeRef

from ..subject_contracts import AudioSubject, SynthesisPart


class AssemblyConfigBase(Contract):
    assembly_id: UUID
    comparison_run_id: UUID
    preferred_lufs: float = Field(default=-20, ge=-40, le=-14)
    true_peak_dbtp: float = Field(default=-1, ge=-6, le=-1)
    headroom_db: float = Field(default=0.3, ge=0.1, le=3)
    join_context_seconds: float = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def unique_parts(self):
        if len(self.part_result_refs) != len(set(self.part_result_refs)):
            raise ValueError("assembly_duplicate_part")
        return self


class AssemblyConfig(AssemblyConfigBase):
    contract: Literal["audio-evaluation-assembly/1"] = "audio-evaluation-assembly/1"
    part_result_refs: list[RuntimeRef] = Field(min_length=1)

    @property
    def bindings(self):
        return [SynthesisPart(kind="comparison_result", ref=ref) for ref in self.part_result_refs]


class AssemblyConfigV2(AssemblyConfigBase):
    contract: Literal["audio-evaluation-assembly/2"] = "audio-evaluation-assembly/2"
    part_sources: list[SynthesisPart] = Field(min_length=1)
    parent_subject: AudioSubject

    @property
    def part_result_refs(self):
        return [p.ref for p in self.part_sources]

    @property
    def bindings(self):
        return self.part_sources


def load_assembly_config(text):
    raw = json.loads(text)
    return (
        AssemblyConfigV2 if raw.get("contract") == "audio-evaluation-assembly/2" else AssemblyConfig
    ).model_validate(raw)


def prefix(assembly_id):
    return "assembly:" + str(assembly_id)
