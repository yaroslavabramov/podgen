from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery("podgen", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "cleanup-expired-temp-sources": {
            "task": "app.workers.ingestion_tasks.cleanup_expired_temp_sources",
            "schedule": crontab(hour=3, minute=0),  # Daily at 3 AM UTC
        },
    },
)

celery_app.autodiscover_tasks(["app.workers"])
