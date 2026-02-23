import uuid
import base64
import binascii
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, status, Request, Response
from sqlalchemy import select

from app.api.deps import CurrentUser, DB
from app.models.project import Project
from app.models.source import Source, SourceType, IngestionStatus
from app.schemas.source import SourceURLCreate, SourceResponse, SourceListResponse
from app.services.storage import get_minio_client, upload_file, generate_object_name

router = APIRouter(prefix="/api", tags=["sources"])

TUS_VERSION = "1.0.0"
TUS_EXPOSE_HEADERS = "Location, Upload-Offset, Upload-Length, Tus-Resumable"
TUS_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "podgen_tus"
TUS_META_ROOT = TUS_UPLOAD_ROOT / "meta"
TUS_FILE_ROOT = TUS_UPLOAD_ROOT / "chunks"


@router.post("/projects/{project_id}/sources/upload", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def upload_source(project_id: uuid.UUID, user: CurrentUser, db: DB, file: UploadFile = File(...)):
    await _verify_project_ownership(project_id, user.id, db)

    # Determine source type from file extension
    filename = file.filename or "unknown"
    source_type = _source_type_from_filename(filename)

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


@router.options("/projects/{project_id}/sources/upload/resumable")
@router.options("/projects/{project_id}/sources/upload/resumable/{upload_id}")
async def tus_options(project_id: uuid.UUID, upload_id: uuid.UUID | None = None):
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_tus_headers({
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": "creation",
            "Access-Control-Allow-Methods": "POST,HEAD,PATCH,OPTIONS",
        }),
    )


@router.post("/projects/{project_id}/sources/upload/resumable", status_code=status.HTTP_201_CREATED)
async def create_resumable_upload(project_id: uuid.UUID, request: Request, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    _require_tus_version(request)

    upload_length = _parse_upload_length(request.headers.get("Upload-Length"))
    metadata = _parse_upload_metadata(request.headers.get("Upload-Metadata", ""))
    filename = metadata.get("filename") or "upload.bin"
    content_type = metadata.get("filetype") or "application/octet-stream"

    upload_id = uuid.uuid4()
    _ensure_tus_dirs()
    temp_path = _tus_file_path(upload_id)
    temp_path.touch(exist_ok=True)

    meta = {
        "upload_id": str(upload_id),
        "project_id": str(project_id),
        "user_id": str(user.id),
        "upload_length": upload_length,
        "offset": 0,
        "filename": filename,
        "content_type": content_type,
        "temp_path": str(temp_path),
        "completed": False,
        "source_id": None,
        "created_at": _utcnow_iso(),
        "updated_at": _utcnow_iso(),
    }
    _write_tus_meta(upload_id, meta)

    # Zero-byte files can be finalized immediately.
    if upload_length == 0:
        source = await _finalize_resumable_upload(meta, db)
        meta["completed"] = True
        meta["source_id"] = str(source.id)
        meta["offset"] = 0
        meta["updated_at"] = _utcnow_iso()
        _write_tus_meta(upload_id, meta)

    location = str(request.url_for("patch_resumable_upload", project_id=str(project_id), upload_id=str(upload_id)))
    return Response(
        status_code=status.HTTP_201_CREATED,
        headers=_tus_headers({
            "Location": location,
            "Upload-Offset": "0",
            "Upload-Length": str(upload_length),
        }),
    )


@router.head("/projects/{project_id}/sources/upload/resumable/{upload_id}")
async def head_resumable_upload(project_id: uuid.UUID, upload_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    meta = _load_tus_meta(upload_id)
    _verify_tus_meta_access(meta, project_id, user.id)

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_tus_headers({
            "Upload-Offset": str(meta["offset"]),
            "Upload-Length": str(meta["upload_length"]),
        }),
    )


@router.patch("/projects/{project_id}/sources/upload/resumable/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def patch_resumable_upload(project_id: uuid.UUID, upload_id: uuid.UUID, request: Request, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    _require_tus_version(request)

    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type != "application/offset+octet-stream":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Content-Type must be application/offset+octet-stream")

    meta = _load_tus_meta(upload_id)
    _verify_tus_meta_access(meta, project_id, user.id)

    current_offset = int(meta["offset"])
    expected_offset = _parse_upload_offset(request.headers.get("Upload-Offset"))
    if expected_offset != current_offset:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            headers=_tus_headers({"Upload-Offset": str(current_offset)}),
        )

    chunk = await request.body()
    upload_length = int(meta["upload_length"])
    new_offset = current_offset + len(chunk)
    if new_offset > upload_length:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Chunk exceeds upload length")

    temp_path = Path(meta["temp_path"])
    if not temp_path.exists():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Upload state expired")

    if len(chunk) > 0:
        with temp_path.open("r+b") as f:
            f.seek(current_offset)
            f.write(chunk)

    meta["offset"] = new_offset
    meta["updated_at"] = _utcnow_iso()

    if new_offset == upload_length and not meta.get("completed", False):
        source = await _finalize_resumable_upload(meta, db)
        meta["completed"] = True
        meta["source_id"] = str(source.id)
        meta["updated_at"] = _utcnow_iso()
        try:
            temp_path.unlink()
        except OSError:
            pass

    _write_tus_meta(upload_id, meta)

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_tus_headers({
            "Upload-Offset": str(new_offset),
            "Upload-Length": str(upload_length),
        }),
    )


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


def _source_type_from_filename(filename: str) -> SourceType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return SourceType.PDF
    if ext in ("mp3", "wav", "m4a", "ogg"):
        return SourceType.AUDIO
    if ext in ("mp4", "webm", "mkv"):
        return SourceType.VIDEO
    return SourceType.PDF


def _ensure_tus_dirs() -> None:
    TUS_META_ROOT.mkdir(parents=True, exist_ok=True)
    TUS_FILE_ROOT.mkdir(parents=True, exist_ok=True)


def _tus_meta_path(upload_id: uuid.UUID) -> Path:
    return TUS_META_ROOT / f"{upload_id}.json"


def _tus_file_path(upload_id: uuid.UUID) -> Path:
    return TUS_FILE_ROOT / f"{upload_id}.bin"


def _write_tus_meta(upload_id: uuid.UUID, payload: dict) -> None:
    _ensure_tus_dirs()
    _tus_meta_path(upload_id).write_text(json.dumps(payload), encoding="utf-8")


def _load_tus_meta(upload_id: uuid.UUID) -> dict:
    path = _tus_meta_path(upload_id)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_tus_meta_access(meta: dict, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if meta.get("project_id") != str(project_id) or meta.get("user_id") != str(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")


def _parse_upload_metadata(raw_header: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not raw_header.strip():
        return parsed

    for item in raw_header.split(","):
        part = item.strip()
        if not part:
            continue
        key, *rest = part.split(" ", 1)
        if not rest:
            parsed[key] = ""
            continue
        try:
            b64_value = rest[0] + "=" * (-len(rest[0]) % 4)
            decoded = base64.b64decode(b64_value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        parsed[key] = decoded
    return parsed


def _parse_upload_length(raw: str | None) -> int:
    if raw is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload-Length header required")
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Upload-Length header")
    if value < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload-Length cannot be negative")
    return value


def _parse_upload_offset(raw: str | None) -> int:
    if raw is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload-Offset header required")
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Upload-Offset header")
    if value < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload-Offset cannot be negative")
    return value


def _require_tus_version(request: Request) -> None:
    if request.headers.get("Tus-Resumable") != TUS_VERSION:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="Unsupported TUS version")


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Tus-Resumable": TUS_VERSION,
        "Access-Control-Expose-Headers": TUS_EXPOSE_HEADERS,
    }
    if extra:
        headers.update(extra)
    return headers


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _finalize_resumable_upload(meta: dict, db: DB) -> Source:
    temp_path = Path(meta["temp_path"])
    if not temp_path.exists():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Upload state expired")

    file_data = temp_path.read_bytes()
    filename = meta.get("filename") or "upload.bin"
    project_id = uuid.UUID(meta["project_id"])
    source_type = _source_type_from_filename(filename)
    object_name = generate_object_name(f"sources/{project_id}", filename)
    client = get_minio_client()
    upload_file(client, file_data, object_name, meta.get("content_type") or "application/octet-stream")

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

    from app.workers.ingestion_tasks import ingest_source
    ingest_source.delay(str(source.id))

    return source
