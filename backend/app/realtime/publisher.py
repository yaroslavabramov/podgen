import json

import redis

from app.config import settings
from app.realtime.events import PipelineEvent

_redis_sync: redis.Redis | None = None


def get_redis_sync() -> redis.Redis:
    global _redis_sync
    if _redis_sync is None:
        _redis_sync = redis.from_url(settings.redis_url)
    return _redis_sync


def publish_event(generation_run_id: str, event: PipelineEvent) -> None:
    r = get_redis_sync()
    channel = f"generation:{generation_run_id}"
    r.publish(channel, json.dumps(event.to_dict()))


def update_generation_progress(generation_run_id: str, phase: str, step: str, pct: float) -> None:
    """Update generation progress in Redis for quick reads (avoids DB hit)."""
    r = get_redis_sync()
    key = f"generation_progress:{generation_run_id}"
    r.hset(key, mapping={"phase": phase, "step": step, "pct": str(pct)})
    r.expire(key, 86400)  # 24h TTL
