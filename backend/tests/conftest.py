import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository


@pytest.fixture(scope="session")
def database():
    url = os.environ.get("PODGEN_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set PODGEN_TEST_DATABASE_URL to a migrated PostgreSQL database; SQLite is not a substitute."
        )
    db = Database(url)
    with db.transaction() as tx:
        assert (
            tx.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011_review_anchor_attempt"
        )
    yield db
    db.engine.dispose()


@pytest.fixture
def repo(database, tmp_path):
    return Repository(database, FileObjectStore(tmp_path / "objects"), uuid4(), purpose="evaluation")


@pytest.fixture
def scope(repo):
    project, episode = uuid4(), uuid4()
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
        query(tx, "INSERT INTO episodes VALUES(:p,:e)", p=project, e=episode)
    return project, episode


@pytest.fixture
def producer():
    return CodeProducer(
        kind="code", component="wp01-synthetic-test", build_id="test-only-v1", operation_id=uuid4()
    )
