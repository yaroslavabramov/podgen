"""Production admission acquires every report URL before freezing the writer's corpus."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from podgen.domain.contracts.common import Contract
from podgen.research.hosted.corpus_acquisition import CorpusAcquisition, acquire_corpus
from podgen.research.hosted.journal import ResearchJournal
from podgen.sources.acquisition.service import acquire_source
from podgen.writing.editorial_contract import Assignment

from .admission import prepare_materials
from .contracts import SourceAssessment


class CompleteResearchPreparation(Contract):
    contract: Literal["research-material-preparation/2"] = "research-material-preparation/2"
    operation_id: UUID
    research_run_id: UUID
    acquisition: CorpusAcquisition
    assignment: Assignment
    source_assessments: list[SourceAssessment] = Field(default_factory=list)


async def prepare_complete_materials(repo, project, config, *, acquire=acquire_source):
    config = CompleteResearchPreparation.model_validate(config)
    journal = ResearchJournal(repo, project, config.research_run_id)
    selection_id = uuid5(config.operation_id, "complete-reported-corpus")
    await acquire_corpus(journal, selection_id, config.acquisition, acquire=acquire)
    return prepare_materials(
        repo,
        project,
        dict(
            operation_id=config.operation_id,
            research_run_id=config.research_run_id,
            source_selection_ids=[selection_id],
            assignment=config.assignment,
            source_assessments=config.source_assessments,
        ),
        preparation=config.model_dump(mode="json"),
    )
