"""One concrete production sequence. Child services retain execution and recovery ownership."""

import asyncio
import time
from copy import deepcopy
from uuid import UUID, uuid5

from podgen.domain.errors import ContractError
from podgen.research.materials.preparation import CompleteResearchPreparation, prepare_complete_materials
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation

from . import audio, research, writing
from .contracts import ProductionConfig
from .control import ProductionPaused, ProductionSuperseded, bind
from .journal import ProductionJournal
from .topic import assignment, initial_research


class EpisodeProduction:
    def __init__(
        self,
        repo,
        project,
        run_id,
        *,
        research_provider,
        model_provider,
        audio_transport,
        acquire=None,
        sleep=time.sleep,
    ):
        self.journal = ProductionJournal(repo, project, run_id)
        self.research_provider, self.audio_transport = research_provider, audio_transport
        self.writer = WritingEvaluation(repo, model_provider, material_acquirer=acquire)
        self.acquire, self.sleep = acquire, sleep
        self.repo, self.project = repo, project

    def _save(self, phase, status="running"):
        self.phase = phase
        return self.journal.transition(self.epoch, self.progress, phase, status)

    def _research(self, run_id, phase):
        jobs = self.progress.setdefault("research_jobs", [])
        if str(run_id) not in jobs:
            if len(jobs) >= self.config.settings.max_research_jobs:
                raise ContractError("episode_research_job_allowance_exhausted")
            jobs.append(str(run_id))
            self._save(phase)
        state = research.advance_job(self.journal, self.config, self.progress, run_id, self.research_provider)
        self.progress["research_status"] = {
            key: state.get(key) for key in ("run_id", "state", "provider_job_id")
        }
        if state["state"] != "completed":
            self._save(phase, "waiting")
            return False
        return True

    def _materials(self):
        research_id = initial_research(self.config).run_id
        config = CompleteResearchPreparation(
            operation_id=uuid5(self.config.run_id, "materials/1"),
            research_run_id=research_id,
            acquisition=self.config.settings.acquisition,
            assignment=assignment(self.config),
        )
        options = dict(acquire=self.acquire) if self.acquire is not None else {}
        ref = asyncio.run(prepare_complete_materials(self.repo, self.project, config, **options))
        self.progress["materials_ref"] = ref.model_dump(mode="json")
        self._save("writing")

    def _writing(self):
        journal = EvaluationJournal(self.repo, self.project)
        run_id = self.progress.get("writing_run_id")
        if run_id is None:
            command = writing.command(self.config, journal, self.progress)
            run_id = str(command.run_id)
            self.progress.setdefault("writing_runs", []).append(run_id)
            self.progress.setdefault("writing_commands", {})[run_id] = command_payload(command)
            self.progress["writing_run_id"] = run_id
            self._save("writing")  # The child identity is durable before its first paid action.
        elif journal.run(UUID(run_id)):
            command = writing.saved_command(self.repo, self.project, UUID(run_id))
        else:
            command = WritingRunCommand.model_validate(self.progress["writing_commands"][run_id])
        try:
            result = self.writer.run(command)
        except RunFailure as error:
            if error.code != "source_material_handoff_pending":
                raise
            handoff = writing.pending_handoff(journal, command.run_id, error.invocation_id)
            self.progress["pending_research_run_id"] = handoff["child_run_id"]
            self._save("research_handoff")
            return
        self.progress.setdefault("writing_results", {})[run_id] = result
        self.progress.setdefault("revisions_by_run", {})[run_id] = writing.completed_revisions(
            journal, command.run_id
        )
        self.progress["revisions_used"] = sum(self.progress["revisions_by_run"].values())
        if self.progress["revisions_used"] > 2:
            raise ContractError("episode_revision_allowance_exhausted")
        if result.get("review_status") == "keep":
            from podgen.runtime.evaluation.reading_outcome import read_outcome

            outcome = read_outcome(journal, command.run_id)
            if outcome is None or outcome["status"] != "keep":
                raise ContractError("episode_native_keep_outcome_missing")
            self.progress["outcome_ref"] = result["outcome_ref"]
            self._save("audio")
            return
        questions, script = writing.material_questions(journal, result)
        if not questions:
            raise ContractError(result.get("failure_code") or "episode_writing_not_kept")
        self.progress["material_questions"] = questions
        self.progress["material_question_index"] = 0
        self.progress["continuation_script_ref"] = script
        self._save("terminal_materials")

    def _terminal_materials(self):
        index = self.progress["material_question_index"]
        questions = self.progress["material_questions"]
        if index == len(questions):
            self.progress.pop("writing_run_id")
            self._save("writing")
            return True
        from .material_requests import VERSION as BOUND_QUESTION

        bound = any(question.get("contract") == BOUND_QUESTION for question in questions)
        question = questions[index] if bound else research.terminal_question(questions[index:])
        result = research.resolve_terminal(
            self.journal,
            self.writer,
            self.progress,
            question,
            guard=lambda: self.journal.check_dispatch(self.epoch),
        )
        if result.get("status") == "pending":
            if not self._research(UUID(result["child_run_id"]), "terminal_materials"):
                return False
            result = research.resolve_terminal(
                self.journal,
                self.writer,
                self.progress,
                question,
                guard=lambda: self.journal.check_dispatch(self.epoch),
            )
        if bound:
            self.progress.setdefault("material_question_results", {}).setdefault(
                self.progress["writing_run_id"], {}
            )[str(index)] = result
        if result.get("status") != "resolved":
            if bound:
                self._save("terminal_materials")
            raise ContractError("episode_terminal_research_unresolved")
        self.progress["material_question_index"] = index + 1 if bound else len(questions)
        self._save("terminal_materials")
        return True

    def run(self, *, wait=True, poll_seconds=15):
        with self.journal.lock():
            row = self.journal.read()
            if row["status"] == "assembled":
                return row
            if row["desired_state"] == "paused":
                return self.journal.transition(row["epoch"], row["progress"], row["phase"], "paused")
            row = self.journal.claim()
            self.config = ProductionConfig.model_validate(row["configuration"])
            self.epoch, self.progress, self.phase = row["epoch"], deepcopy(row["progress"]), row["phase"]
            self.progress.pop("error_code", None)
            with bind(self.journal, self.epoch):
                try:
                    while True:
                        self.journal.check_dispatch(self.epoch)
                        ready = True
                        if self.phase == "research":
                            ready = self._research(initial_research(self.config).run_id, "research")
                            if ready:
                                self._save("materials")
                        elif self.phase == "materials":
                            self._materials()
                        elif self.phase == "writing":
                            self._writing()
                        elif self.phase == "research_handoff":
                            ready = self._research(
                                UUID(self.progress["pending_research_run_id"]), "research_handoff"
                            )
                            if ready:
                                self._save("writing")
                        elif self.phase == "terminal_materials":
                            ready = self._terminal_materials()
                        elif self.phase == "audio":
                            _, _, ref = audio.render(
                                self.journal, self.config, self.progress, self.audio_transport
                            )
                            self.progress["assembly_ref"] = ref
                            return self._save("assembled", "assembled")
                        else:
                            raise ContractError("episode_unknown_phase")
                        if not ready:
                            if not wait:
                                return self.journal.read()
                            self.sleep(poll_seconds)
                except ProductionPaused:
                    return self._save(self.phase, "paused")
                except ProductionSuperseded:
                    return self.journal.read()
                except Exception as error:
                    self.progress["error_code"] = getattr(error, "code", type(error).__name__)
                    self._save(self.phase, "needs_attention")
                    raise
