"""Export exact child artifacts and an assembled, acoustically unreviewed master."""

from pathlib import Path
from uuid import UUID

from podgen.domain.errors import ContractError
from podgen.research.hosted.export import export as export_research
from podgen.research.hosted.journal import ResearchJournal
from podgen.runtime.evaluation.audio.assembly.export import export as export_assembly
from podgen.runtime.evaluation.audio.export_directory import bind_directory, write_json
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.export import export_run

from .audio import configuration
from .contracts import ProductionConfig


def status(row):
    progress = row["progress"]
    return dict(
        run_id=str(row["run_id"]),
        topic=row["configuration"]["topic"],
        state=row["status"],
        phase=row["phase"],
        desired_state=row["desired_state"],
        research=progress.get("research_status"),
        writing_run_id=progress.get("writing_run_id"),
        revisions_used=progress.get("revisions_used", 0),
        max_script_revisions=2,
        error_code=progress.get("error_code") if row["status"] == "needs_attention" else None,
        assembly_ref=progress.get("assembly_ref"),
        acoustic_review="pending",
        production_ready=False,
    )


def export(journal, folder):
    from uuid import uuid5

    from podgen.domain.contracts.common import RuntimeRef
    from podgen.runtime.evaluation.audio.assembly.contracts import AssemblyConfig
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.reading_outcome import read_outcome

    row = journal.read()
    config, progress = ProductionConfig.model_validate(row["configuration"]), row["progress"]
    folder = Path(folder)
    bind_directory(
        folder,
        "episode-binding.json",
        dict(
            run_id=str(journal.run_id),
            project_id=str(journal.project),
            configuration_hash=row["configuration_hash"],
        ),
        row["configuration"],
    )
    write_json(folder / "private/manifest.json", row["configuration"])
    for research_id in progress.get("research_jobs", []):
        export_research(
            ResearchJournal(journal.repo, journal.project, UUID(research_id)),
            folder / "research" / research_id,
        )
    for run_id in progress.get("writing_runs", []):
        writing = EvaluationJournal(journal.repo, journal.project)
        if writing.run(UUID(run_id)):
            export_run(journal.repo, journal.project, UUID(run_id), folder / "writing" / run_id)
    if row["status"] in ("assembled", "accepted"):
        writing = EvaluationJournal(journal.repo, journal.project)
        outcome = read_outcome(writing, UUID(progress["writing_run_id"]))
        if outcome is None or outcome["status"] != "keep":
            raise ContractError("episode_export_requires_native_keep_outcome")
        render = configuration(config, RuntimeRef.model_validate(progress["outcome_ref"]), outcome["output"])
        audio = AudioJournal(journal.repo, journal.project, render.run_id)
        from podgen.runtime.evaluation.audio.assembly.contracts import prefix

        assembly_id = uuid5(journal.run_id, "audio-assembly/1")
        manifest = audio.get(prefix(assembly_id) + ":manifest")
        assembly = AssemblyConfig.model_validate(manifest["configuration"])
        exported = export_assembly(audio, assembly, folder / "audio")
        if exported["assembly_ref"] != progress["assembly_ref"]:
            raise ContractError("episode_export_assembly_mismatch")
    value = status(row)
    write_json(folder / "status.json", value)
    return value
