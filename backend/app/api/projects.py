import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, func

from app.api.deps import CurrentUser, DB
from app.models.project import Project
from app.models.source import Source
from app.models.episode import Episode
from app.schemas.project import ProjectCreate, ProjectUpdate, ProjectResponse, ProjectDetailResponse, ProjectListResponse

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(data: ProjectCreate, user: CurrentUser, db: DB):
    project = Project(
        user_id=user.id,
        title=data.title,
        description=data.description,
        language=data.language,
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("", response_model=ProjectListResponse)
async def list_projects(user: CurrentUser, db: DB):
    result = await db.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.updated_at.desc())
    )
    projects = result.scalars().all()

    items = []
    for p in projects:
        source_count = (await db.execute(
            select(func.count(Source.id)).where(Source.project_id == p.id)
        )).scalar() or 0
        episode_count = (await db.execute(
            select(func.count(Episode.id)).where(Episode.project_id == p.id)
        )).scalar() or 0
        resp = ProjectResponse.model_validate(p)
        resp.source_count = source_count
        resp.episode_count = episode_count
        items.append(resp)

    return ProjectListResponse(items=items, total=len(items))


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project(project_id: uuid.UUID, user: CurrentUser, db: DB):
    from app.models.show_bible import ShowBible

    project = await _get_user_project(project_id, user.id, db)

    # Sources (non-temporary)
    sources_result = await db.execute(
        select(Source).where(Source.project_id == project.id, Source.is_temporary == False)
        .order_by(Source.created_at.desc())
    )
    sources = sources_result.scalars().all()

    # Show Bible (project-level only, exclude run-scoped copies)
    bible_result = await db.execute(
        select(ShowBible).where(
            ShowBible.project_id == project.id,
            ShowBible.is_preset == False,
            ShowBible.generation_run_id.is_(None),
        )
    )
    show_bible = bible_result.scalar_one_or_none()

    source_count = len(sources)
    episode_count = (await db.execute(
        select(func.count(Episode.id)).where(Episode.project_id == project.id)
    )).scalar() or 0

    response_sources = [
        {
            "id": str(s.id),
            "source_type": s.source_type,
            "title": s.title,
            "original_url": s.original_url,
            "ingestion_status": s.ingestion_status,
            "domain_reliability": s.domain_reliability,
            "created_at": s.created_at.isoformat(),
        }
        for s in sources
    ]

    response_show_bible = None
    if show_bible:
        response_show_bible = {
            "id": str(show_bible.id),
            "show_style": show_bible.show_style,
            "tone_and_mood": show_bible.tone_and_mood,
            "audience_profile": show_bible.audience_profile,
            "hosts": show_bible.hosts,
            "format_rules": show_bible.format_rules,
        }

    return ProjectDetailResponse(
        id=project.id,
        user_id=project.user_id,
        title=project.title,
        description=project.description,
        language=project.language,
        created_at=project.created_at,
        updated_at=project.updated_at,
        source_count=source_count,
        episode_count=episode_count,
        sources=response_sources,
        show_bible=response_show_bible,
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: uuid.UUID, data: ProjectUpdate, user: CurrentUser, db: DB):
    project = await _get_user_project(project_id, user.id, db)
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(project, key, value)
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, user: CurrentUser, db: DB):
    project = await _get_user_project(project_id, user.id, db)
    await db.delete(project)


async def _get_user_project(project_id: uuid.UUID, user_id: uuid.UUID, db) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project
