import uuid

from fastapi import APIRouter, HTTPException, UploadFile, File, status
from sqlalchemy import select, func

from app.api.deps import CurrentUser, DB
from app.models.project import Project
from app.models.source import Source, SourceType, IngestionStatus
from app.schemas.source import SourceURLCreate, SourceResponse, SourceListResponse
from app.services.storage import get_minio_client, upload_file, generate_object_name

router = APIRouter(prefix="/api", tags=["sources"])


@router.post("/projects/{project_id}/sources/upload", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def upload_source(project_id: uuid.UUID, user: CurrentUser, db: DB, file: UploadFile = File(...)):
    await _verify_project_ownership(project_id, user.id, db)

    # Determine source type from file extension
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    source_type = SourceType.PDF if ext == "pdf" else SourceType.AUDIO if ext in ("mp3", "wav", "m4a", "ogg") else SourceType.VIDEO if ext in ("mp4", "webm", "mkv") else SourceType.PDF

    # Upload to MinIO
    file_data = await file.read()
    object_name = generate_object_name(f"sources/{project_id}", filename)
    client = get_minio_client()
    upload_file(client, file_data, object_name, file.content_type or "application/octet-stream")

    source = Source(
        project_id=project_id,
        source_type=source_type,
        file_path=object_name,
        title=filename,
        ingestion_status=IngestionStatus.PENDING,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)

    # Trigger async ingestion
    from app.workers.ingestion_tasks import ingest_source
    ingest_source.delay(str(source.id))

    return SourceResponse.model_validate(source)


@router.post("/projects/{project_id}/sources/url", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def add_source_url(project_id: uuid.UUID, data: SourceURLCreate, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)

    source_type_map = {"web": SourceType.WEB, "video": SourceType.VIDEO, "audio": SourceType.AUDIO}
    source = Source(
        project_id=project_id,
        source_type=source_type_map.get(data.source_type, SourceType.WEB),
        original_url=data.url,
        ingestion_status=IngestionStatus.PENDING,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)

    from app.workers.ingestion_tasks import ingest_source
    ingest_source.delay(str(source.id))

    return SourceResponse.model_validate(source)


@router.get("/projects/{project_id}/sources", response_model=SourceListResponse)
async def list_sources(project_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    result = await db.execute(
        select(Source).where(Source.project_id == project_id, Source.is_temporary == False).order_by(Source.created_at.desc())
    )
    sources = result.scalars().all()
    return SourceListResponse(items=[SourceResponse.model_validate(s) for s in sources], total=len(sources))


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, user: CurrentUser, db: DB):
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    # Verify ownership through project
    await _verify_project_ownership(source.project_id, user.id, db)
    await db.delete(source)


@router.get("/sources/{source_id}/status", response_model=SourceResponse)
async def get_source_status(source_id: uuid.UUID, user: CurrentUser, db: DB):
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    await _verify_project_ownership(source.project_id, user.id, db)
    return SourceResponse.model_validate(source)


async def _verify_project_ownership(project_id: uuid.UUID, user_id: uuid.UUID, db):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
