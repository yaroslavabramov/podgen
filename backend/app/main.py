from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from app.services.embeddings import ensure_collection
    ensure_collection()
    yield
    # Shutdown
    from app.database import engine
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Podgen API",
        version="0.1.0",
        description="AI Podcast Generation Platform",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    from app.api.projects import router as projects_router
    from app.api.show_bibles import router as show_bibles_router
    from app.api.sources import router as sources_router
    from app.api.episodes import router as episodes_router
    from app.api.generations import router as generations_router
    from app.api.ws import router as ws_router

    app.include_router(projects_router)
    app.include_router(show_bibles_router)
    app.include_router(sources_router)
    app.include_router(episodes_router)
    app.include_router(generations_router)
    app.include_router(ws_router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/auth/dev-login")
    async def dev_login(request_data: dict):
        """Development-only login: creates user if not exists, returns JWT."""
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models.user import User
        from app.api.deps import create_access_token
        import uuid

        email = request_data.get("email", "")
        name = request_data.get("name", email.split("@")[0])

        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if not user:
                user = User(id=uuid.uuid4(), email=email, name=name, provider="dev")
                db.add(user)
                await db.commit()
                await db.refresh(user)

        token = create_access_token(str(user.id))
        return {"token": token, "user": {"id": str(user.id), "email": user.email, "name": user.name}}

    @app.get("/api/voices")
    async def list_voices():
        """List available ElevenLabs voices (cached)."""
        try:
            from elevenlabs import ElevenLabs
            client = ElevenLabs(api_key=settings.elevenlabs_api_key)
            voices = client.voices.get_all()
            return [
                {"voice_id": v.voice_id, "name": v.name, "category": v.category, "labels": v.labels}
                for v in voices.voices
            ]
        except Exception:
            return []

    return app


app = create_app()
