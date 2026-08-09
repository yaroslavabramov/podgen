from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_completion_handlers import (
    SourceTriageWorkspaceCompletionHandlersMixin,
)
from app.generation_v2.source_triage_workspace_coverage_hint_handlers import (
    SourceTriageWorkspaceCoverageHintHandlersMixin,
)
from app.generation_v2.source_triage_workspace_finding_handlers import (
    SourceTriageWorkspaceFindingHandlersMixin,
)
from app.generation_v2.source_triage_workspace_grounding_audit_handlers import (
    SourceTriageWorkspaceGroundingAuditHandlersMixin,
)
from app.generation_v2.source_triage_workspace_outcome_handlers import (
    SourceTriageWorkspaceOutcomeHandlersMixin,
)
from app.generation_v2.source_triage_workspace_planning_handlers import (
    SourceTriageWorkspacePlanningHandlersMixin,
)
from app.generation_v2.source_triage_workspace_reading_handlers import (
    SourceTriageWorkspaceReadingHandlersMixin,
)
from app.generation_v2.source_triage_workspace_review_handlers import (
    SourceTriageWorkspaceReviewHandlersMixin,
)
from app.generation_v2.source_triage_workspace_source_analysis_handlers import (
    SourceTriageWorkspaceSourceAnalysisHandlersMixin,
)
from app.generation_v2.source_triage_workspace_support_handlers import (
    SourceTriageWorkspaceSupportHandlersMixin,
)
from app.generation_v2.source_triage_workspace_synthesis_handlers import (
    SourceTriageWorkspaceSynthesisHandlersMixin,
)
from app.models.generation_run import GenerationRun


@dataclass
class SourceTriageWorkspaceToolHandlers(
    SourceTriageWorkspacePlanningHandlersMixin,
    SourceTriageWorkspaceReadingHandlersMixin,
    SourceTriageWorkspaceReviewHandlersMixin,
    SourceTriageWorkspaceSourceAnalysisHandlersMixin,
    SourceTriageWorkspaceCoverageHintHandlersMixin,
    SourceTriageWorkspaceFindingHandlersMixin,
    SourceTriageWorkspaceGroundingAuditHandlersMixin,
    SourceTriageWorkspaceOutcomeHandlersMixin,
    SourceTriageWorkspaceSynthesisHandlersMixin,
    SourceTriageWorkspaceSupportHandlersMixin,
    SourceTriageWorkspaceCompletionHandlersMixin,
):
    db: AsyncSession
    run: GenerationRun
    workspace: dict[str, Any]
    inventory: SourceMap
    evidence_toolset: Any | None = None
    current_set_id: str | None = None


__all__ = ["SourceTriageWorkspaceToolHandlers"]
