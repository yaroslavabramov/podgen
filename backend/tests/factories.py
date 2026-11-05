"""SYNTHETIC engineering data. No expert verification or production qualification."""

import io
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.episodes.branches import EditorialBranches
from podgen.persistence.repository import timestamp

QUESTION = "Как GPS-приёмник определяет положение, если его часы неточны?"
ANSWER = (
    "Приёмник совместно оценивает три координаты и общее смещение своих часов. "
    "Поэтому измеренный путь называют псевдодальностью: в него входит ошибка часов приёмника. "
    "В обычной трёхмерной постановке четыре независимых измерения дают четыре уравнения. "
    "Это не означает, что любые четыре спутника всегда обеспечат точное решение: важны геометрия "
    "и ошибки измерений. Здесь описан учебный упрощённый случай, а не все режимы GNSS."
)


class Factory:
    def __init__(self, repo, project, episode, producer):
        self.repo, self.project, self.episode, self.producer = repo, project, episode, producer

    def make(self, kind, payload, **kwargs):
        return self.repo.create(
            self.project,
            kind,
            payload,
            producer=self.producer,
            operation_id=uuid4(),
            episode_id=self.episode,
            **kwargs,
        )

    def setup(self):
        self.policies = {
            kind: self.make("Policy", dict(policy_kind=kind, rules={"test_data": True})).ref.model_dump(
                mode="json"
            )
            for kind in ("source", "quality", "audio", "data", "operations", "evaluation")
        }
        self.bundle = self.make(
            "PolicyBundle",
            {
                f"{k}_policy_ref": self.policies[k]
                for k in ("source", "quality", "audio", "data", "operations")
            },
        )
        self.model = self.make(
            "ModelProfile",
            dict(
                provider="test-only",
                api_family="synthetic",
                model_id="no-real-model",
                model_revision_or_alias="test-v1",
                adapter_version="test-v1",
                allowed_roles=["writer"],
                supported_modalities=["text"],
                context_limit=100000,
                output_limit=10000,
                structured_output_mode="json",
                tool_call_mode="none",
                parallel_tool_policy="none",
                generation_parameters={},
                timeout_policy={},
                data_retention_policy_ref=self.policies["data"],
                caching_policy={},
                fallback_profile_refs=[],
                configured_languages=["ru"],
            ),
        )
        self.voice = self.make(
            "VoiceProfile",
            dict(
                provider="test-only",
                provider_voice_id="synthetic-no-audio",
                provider_revision_or_alias="test-v1",
                model_profile_ref=self.model.ref.model_dump(mode="json"),
                language_locales=["ru-RU"],
                voice_kind="stock",
                authorization_ref=None,
                usage_policy_ref=self.policies["data"],
                limitations=["ТЕСТОВЫЕ ДАННЫЕ: звук не синтезирован"],
            ),
        )
        self.speakers = [uuid4(), uuid4()]
        self.spec = self.make(
            "EpisodeSpec",
            dict(
                topic=QUESTION,
                main_question=QUESTION,
                audience=dict(description="Любознательный слушатель", assumed_knowledge=[]),
                language="ru",
                format="dialogue",
                speakers=[
                    dict(
                        speaker_id=str(s),
                        display_name=name,
                        role_description=role,
                        voice_profile_ref=self.voice.ref.model_dump(mode="json"),
                    )
                    for s, name, role in zip(
                        self.speakers,
                        ["Анна", "Борис"],
                        ["Задаёт вопрос и уточняет объяснение", "Объясняет механизм"],
                        strict=True,
                    )
                ],
                duration=dict(target_seconds=1800, tolerance_seconds=120, mode="target"),
                temporal=dict(
                    mode="current", requested_as_of=None, knowledge_as_of=timestamp(), subject_period=None
                ),
                source_mode="supplied_only",
                supplied_inputs=[],
                preferences=["ТЕСТОВЫЕ инженерные данные"],
                required_topics=[],
                excluded_topics=[],
                policy_ref=self.bundle.ref.model_dump(mode="json"),
                source_policy_ref=self.policies["source"],
                delivery=dict(audio_formats=["mp3"], transcript_formats=["json", "vtt"]),
            ),
        )
        self.branch_id = uuid4()
        self.state = EditorialBranches(self.repo).bootstrap(
            self.project,
            self.episode,
            self.spec.ref,
            producer=self.producer,
            operation_id=uuid4(),
            branch_id=self.branch_id,
        )
        return self

    def note(self, text=ANSWER):
        return self.make(
            "ResearchNote",
            dict(
                question_refs=[],
                title="ТЕСТОВАЯ заметка: часы GPS",
                answer_markdown=text,
                citations=[],
                interpretations=[],
                limitations=["ТЕСТОВЫЕ ДАННЫЕ: не экспертное досье"],
                uncertainty_refs=[],
                supersedes_note_refs=[],
            ),
        )

    def dossier(self, note):
        return self.make(
            "DossierSnapshot",
            dict(
                episode_spec_ref=self.spec.ref.model_dump(mode="json"),
                knowledge_as_of=self.spec.payload["temporal"]["knowledge_as_of"],
                source_policy_ref=self.policies["source"],
                overview_note_ref=note.ref.model_dump(mode="json"),
                timeline_note_ref=None,
                note_refs=[note.ref.model_dump(mode="json")],
                question_refs=[],
                uncertainty_refs=[],
                source_revision_refs=[],
                evidence_refs=[],
                candidate_materials=[],
                excluded_topics=[],
            ),
        )

    def decision(self):
        return self.make(
            "EditorialDecision",
            dict(
                state_ref=self.state.ref.model_dump(mode="json"),
                action="commission",
                reason="ТЕСТОВОЕ поручение для проверки контрактов",
                considered_options=[],
                selected_option="Объяснить неизвестное смещение часов",
                input_result_ids=[],
                question_refs=[],
                reviewed_output_refs=[],
                dependency_dispositions=[],
                request_dispositions=[],
                expected_effect="Связное объяснение вопроса",
                remaining_risks=["Не экспертное оценивание"],
            ),
        )

    def plan(self, dossier):
        option, act = uuid4(), uuid4()
        decision = self.decision()
        return self.make(
            "StoryPlan",
            dict(
                episode_spec_ref=self.spec.ref.model_dump(mode="json"),
                dossier_ref=dossier.ref.model_dump(mode="json"),
                editorial_decision_ref=decision.ref.model_dump(mode="json"),
                main_question=QUESTION,
                listener_promise="Объяснить, зачем решать ещё и смещение часов",
                working_answer=ANSWER,
                selected_option_id=str(option),
                considered_options=[
                    dict(
                        option_id=str(option),
                        angle="От ошибки часов к четвёртой неизвестной",
                        route_summary="Вопрос — псевдодальность — совместное решение",
                        strongest_material_refs=[dossier.ref.model_dump(mode="json")],
                        listener_benefit="Понимание механизма",
                        weakness="Упрощённая модель",
                        research_needed=[],
                        selection_reason="Прямой ответ",
                    )
                ],
                opening=dict(approach="Вопрос ведущей", material_refs=[], question_to_open=QUESTION),
                acts=[
                    dict(
                        plan_act_id=str(act),
                        title="Четвёртая неизвестная",
                        listener_question=QUESTION,
                        intended_change_in_understanding="Ошибка часов становится неизвестной величиной",
                        prerequisite_concepts=[],
                        material_refs=[dossier.ref.model_dump(mode="json")],
                        limitations_to_voice=["Учебное упрощение"],
                        transitions=dict(from_previous="", toward_next="Ответ"),
                        promises_opened=[],
                        promises_addressed=[],
                        estimated_seconds_range=dict(min_seconds=60, max_seconds=120),
                        priority="essential",
                    )
                ],
                ending=dict(
                    answer_to_deliver=ANSWER,
                    qualifications=["Не любые четыре спутника"],
                    promises_to_close=[],
                ),
                host_functions=[
                    dict(
                        speaker_id=str(s),
                        contribution=role,
                        interaction_guidance="Отвечать на предыдущую реплику",
                        speech_register="Разговорный",
                    )
                    for s, role in zip(self.speakers, ["Уточнять", "Объяснять"], strict=True)
                ],
                duration_strategy=dict(
                    estimated_seconds_range=dict(min_seconds=60, max_seconds=120),
                    assumptions=["ТЕСТОВЫЙ фрагмент; не полный выпуск"],
                    optional_material=[],
                ),
                unresolved_research_request_refs=[],
            ),
        )

    def script(self, dossier=None, plan=None, *, parent=None, text=ANSWER, input_refs=()):
        payload = dict(
            episode_spec_ref=self.spec.ref.model_dump(mode="json"),
            story_plan_ref=plan.ref.model_dump(mode="json") if plan else None,
            dossier_ref=dossier.ref.model_dump(mode="json") if dossier else None,
            parent_script_ref=parent.ref.model_dump(mode="json") if parent else None,
            mode="production" if plan else "exploratory",
            completion="complete",
            acts=[
                dict(
                    act_id=str(uuid4()),
                    plan_act_id=plan.payload["acts"][0]["plan_act_id"] if plan else None,
                    title="ТЕСТОВЫЙ диалог",
                    draft_tail="closed",
                    blocks=[
                        dict(
                            block_id=str(uuid4()),
                            function_note=None,
                            items=[
                                dict(
                                    kind="utterance",
                                    utterance_id=str(uuid4()),
                                    speaker_id=str(self.speakers[0]),
                                    text=QUESTION,
                                ),
                                dict(
                                    kind="stage_direction",
                                    direction_id=str(uuid4()),
                                    purpose="ТЕСТ: не произносить",
                                    text="Объяснить спокойно",
                                ),
                                dict(
                                    kind="utterance",
                                    utterance_id=str(uuid4()),
                                    speaker_id=str(self.speakers[1]),
                                    text=text,
                                ),
                            ],
                        )
                    ],
                )
            ],
            author_support_bindings=[],
            plan_departures=[],
            open_research_request_refs=[],
        )
        return self.make(
            "ScriptRevision",
            payload,
            input_refs=input_refs,
            artifact_id=parent.ref.artifact_id if parent else None,
        )

    def source(self, text=ANSWER):
        blob = self.repo.save_blob(self.project, io.BytesIO(text.encode()), "text/plain; charset=utf-8")
        runtime = RuntimeRef(record_kind="acquisition_record", record_id=uuid4())
        self.repo.append_runtime(self.project, runtime, {"test_data": True})
        source = self.make(
            "SourceRevision",
            dict(
                source_id=str(uuid4()),
                original_blob=blob.model_dump(mode="json"),
                requested_uri="test://gps-fixture",
                resolved_uri="test://gps-fixture",
                redirect_chain=[],
                acquired_at=timestamp(),
                response_metadata=dict(status=200, content_type="text/plain"),
                creators=[],
                acquisition_kind="upload",
                acquisition_record_ref=runtime.model_dump(mode="json"),
                rights=dict(
                    access_class="project",
                    storage_policy_ref=self.policies["data"],
                    publication_policy_ref=self.policies["data"],
                ),
                source_metadata_evidence=[],
                limitations=["ТЕСТОВЫЕ инженерные данные, не первоисточник"],
            ),
        )
        return source, blob

    def evaluation_result(self, profile, companion_profiles=()):
        profile_refs = [p.ref.model_dump(mode="json") for p in (profile, *companion_profiles)]
        annotation = self.make(
            "EvaluationAnnotation",
            dict(
                episode_spec_ref=self.spec.ref.model_dump(mode="json"),
                source_snapshot_refs=[],
                source_assertions=[],
                expected_understanding=[],
                forbidden_inferences=[],
                known_uncertainties=[],
                scoring_policy_ref=self.policies["evaluation"],
                annotator_assignment_ids=[],
            ),
        )
        case = self.make(
            "EvaluationCase",
            dict(
                case_family="test-only-gps",
                split="development",
                topic_group_id="test-only-gps",
                mode="component",
                episode_spec_ref=self.spec.ref.model_dump(mode="json"),
                input_refs=[],
                source_access_policy_ref=self.policies["source"],
                reference_annotation_ref=annotation.ref.model_dump(mode="json"),
                scoring_policy_ref=self.policies["evaluation"],
                allowed_candidate_profile_refs=profile_refs,
                supported_combination=dict(language="ru", duration_seconds=60, format="dialogue"),
                perturbations=[],
                prohibited_shortcuts=["SYNTHETIC TEST: no production qualification claim"],
            ),
        )
        return self.make(
            "EvaluationResult",
            dict(
                case_ref=case.ref.model_dump(mode="json"),
                candidate_profile_refs=profile_refs,
                repeat_index=0,
                output_refs=[],
                run_ids=[],
                evaluator_profile_refs=[],
                measurements=[{"test_data": True, "tested_roles": ["writer"]}],
                human_assessment_refs=[],
                findings=[],
                outcome="pass",
                exclusions_with_reasons=[],
                started_at=timestamp(),
                completed_at=timestamp(),
            ),
        )
