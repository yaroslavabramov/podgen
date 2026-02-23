from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

# Celery tasks run async code via asyncio.run(), which creates a fresh event loop
# per task execution. Using NullPool avoids reusing asyncpg connections that are
# bound to a previous event loop.
worker_engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
worker_session_factory = async_sessionmaker(worker_engine, class_=AsyncSession, expire_on_commit=False)
