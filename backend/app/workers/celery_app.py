from celery import Celery
from celery.schedules import crontab

from app.config import settings

redis_visibility_timeout = max(
    int(settings.celery_redis_visibility_timeout_seconds),
    int(settings.generation_v2_worker_hard_time_limit_seconds) * 2,
)

celery_app = Celery(
    "podgen",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.ingestion_tasks",
        "app.workers.generation_tasks",
    ],
)

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
    broker_transport_options={"visibility_timeout": redis_visibility_timeout},
    result_backend_transport_options={"visibility_timeout": redis_visibility_timeout},
    visibility_timeout=redis_visibility_timeout,
    beat_schedule={
        "cleanup-expired-temp-sources": {
            "task": "app.workers.ingestion_tasks.cleanup_expired_temp_sources",
            "schedule": crontab(minute=0),  # Every hour
        },
        "sweep-dirty-content-maps": {
            "task": "app.workers.ingestion_tasks.sweep_dirty_content_maps",
            "schedule": crontab(minute="*/5"),  # Backstop for missed debounce rebuilds
        },
        "recover-stale-generation-v2-runs": {
            "task": "app.workers.generation_tasks.recover_stale_generation_v2_runs",
            "schedule": crontab(minute="*"),
        },
    },
)

celery_app.autodiscover_tasks(["app.workers"])

# Ensure task modules are imported so Celery registers task names
# like `app.workers.ingestion_tasks.ingest_source`.
from app.workers import generation_tasks as _generation_tasks  # noqa: F401,E402
from app.workers import ingestion_tasks as _ingestion_tasks  # noqa: F401,E402
