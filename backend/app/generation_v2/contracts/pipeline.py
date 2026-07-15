from __future__ import annotations

from enum import StrEnum


class V2Phase(StrEnum):
    SOURCE_TRIAGE = "v2_source_triage"
    SOURCE_ACQUISITION = "v2_source_acquisition"
    EDITORIAL_RESEARCH = "v2_editorial_research"
    RESEARCH_AUDIT = "v2_research_audit"
    TREATMENT = "v2_treatment"
    ACT_DRAFTING = "v2_act_drafting"
    CRITIQUE = "v2_critique"
    CRITIQUE_REWRITE = "v2_critique_rewrite"
    FACT_CITATION = "v2_fact_citation"
    SUPPORT_JUDGMENT = "v2_support_judgment"
    CORPUS_PREPARE = "v2_corpus_prepare"
    CORPUS_EVIDENCE = "v2_corpus_evidence"
    SOURCE_WORKSPACE = "v2_source_workspace"


class V2PipelineStep(StrEnum):
    CORPUS_PREPARE = "corpus_prepare"
    SOURCE_TRIAGE = "source_triage"
    EDITORIAL_RESEARCH = "editorial_research"
    RESEARCH_AUDIT = "research_audit"
    TREATMENT = "treatment"
    CRITIQUE = "critique"
    FACT_CITATION = "fact_citation"
    AUDIO = "audio"


__all__ = ["V2Phase", "V2PipelineStep"]
