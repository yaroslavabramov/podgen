"""Phase 3.2B: Deep Web Ingestion trigger (Celery async)."""

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def trigger_deep_ingestion(source_id: str, generation_run_id: str) -> str:
    """Trigger Celery task for deep web ingestion. Returns Celery task ID."""
    from app.workers.ingestion_tasks import deep_ingest_url
    result = deep_ingest_url.delay(source_id, generation_run_id)
    logger.info("Triggered deep ingestion for source %s, task %s", source_id, result.id)
    return result.id
