"""Provider-free preparation validates exact settings and reuses the research instruction."""

from podgen.domain.contracts.common import ArtifactRef
from podgen.persistence.repository import Repository
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.service import prepare as prepare_research
from podgen.runtime.evaluation.profiles import load_profile
from podgen.sources.acquisition.profile import load_profile as acquisition_profile
from podgen.writing.behavior import resolve_behavior

from .contracts import ProductionConfig
from .journal import ProductionJournal
from .topic import initial_research


def prepare(repo, project, config):
    config = ProductionConfig.model_validate(config)
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    settings = config.settings
    for ref, stage in (
        (settings.writer_profile_ref, "draft"),
        (settings.reviewer_profile_ref, "fact-review"),
        (settings.planner_profile_ref, "plan"),
        (settings.plan_reviewer_profile_ref, "plan-review"),
    ):
        resolve_behavior().validate_model(stage, load_profile(generation, project, ref, stage))
    acquisition_profile(repo, project, settings.acquisition.profile_ref)
    for ref in (
        settings.acquisition.rights.storage_policy_ref,
        settings.acquisition.rights.publication_policy_ref,
    ):
        repo.get(project, ArtifactRef.model_validate(ref))
    journal = ProductionJournal(repo, project, config.run_id)
    row = journal.prepare(config)
    research = initial_research(config)
    prepare_research(ResearchJournal(repo, project, research.run_id), research)
    return row
