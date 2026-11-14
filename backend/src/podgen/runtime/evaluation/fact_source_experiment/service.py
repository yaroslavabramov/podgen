"""Execute only the approved initial fact reading through the ordinary role/tool loop."""

from datetime import datetime, timezone
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict
from podgen.research.materials.resolver import ResearchMaterialResolver
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.research_materials.fact_source_mode import VERSION
from podgen.writing.source_tools import SourceCorpus

from ..contracts import RunFailure
from ..export import export_run
from ..operation_guard import OperationGuard
from ..reading_stage import execute_reading_stage
from ..service import WritingEvaluation
from ..streaming_search_index import StreamingSearchIndex
from .preparation import admit, prepare


class ApprovalBoundGateway(ModelGateway):
    def __init__(self, *args, approved_request_hash=None, **options):
        super().__init__(*args, **options)
        self.approved_request_hash = approved_request_hash

    def invoke(self, invocation, *args, **options):
        if digest(self.journal.request(invocation)) != self.approved_request_hash:
            raise Conflict("fact_experiment_request_not_approved")
        return super().invoke(invocation, *args, **options)


def run(
    repo,
    generation,
    journal,
    source_run_id,
    run_id,
    provider_factory,
    approved_request_hash,
    metrics,
    directory,
):
    with journal.lock(run_id):
        with metrics.phase("whole_review"):
            with metrics.phase("startup"):
                preview = prepare(repo, generation, journal, source_run_id, run_id)
                if digest(preview.body) != approved_request_hash:
                    raise Conflict("fact_experiment_request_not_approved")
                current = admit(repo, journal, preview)
                command, manifest, materials = current.command, current.manifest, current.materials

                def checkpoint():
                    elapsed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
                    ).total_seconds()
                    if elapsed > command.limits.max_elapsed_seconds:
                        raise RunFailure("run_elapsed_budget_exceeded", run_id=run_id)

                loader = WritingEvaluation(repo, provider_factory)
                guard = OperationGuard(
                    generation=generation,
                    journal=journal,
                    command=command,
                    manifest=manifest,
                    materials=materials,
                    loader=loader._materials,
                    checkpoint=checkpoint,
                )
                guard()
                profile = manifest["source_tool_contract"]["retrieval"]
                index = StreamingSearchIndex(journal, materials.shared_ref, profile, run_id=run_id)
                index.access.run_id = run_id
                corpus = SourceCorpus(materials, retrieval=profile, index=index, fact_input_mode=VERSION)
                corpus.bind_execution(journal.execution, run_id)
                corpus.material_resolver = ResearchMaterialResolver(journal, materials)
                metrics.instrument_search(corpus)
                gateway = ApprovalBoundGateway(
                    journal,
                    provider_factory,
                    source_corpus=corpus,
                    approved_request_hash=approved_request_hash,
                )
            try:
                journal.status(run_id, "running")
                with metrics.phase("fact_review"):
                    result = execute_reading_stage(
                        "fact-review",
                        journal=journal,
                        gateway=gateway,
                        generation=generation,
                        command=command,
                        manifest=manifest,
                        materials=materials,
                        totals=dict(input=0, output=0),
                        guard=guard,
                        script=current.script,
                        script_ref=current.script_ref,
                        approved_plan=current.approved_plan,
                    )
                journal.status(run_id, "completed_unaccepted")
                return result
            except RunFailure as error:
                journal.status(run_id, "needs_attention", error.code)
                raise
            finally:
                with metrics.phase("export"):
                    export_run(repo, journal.project, run_id, Path(directory) / "export")
