"""Reproducible synthetic GPS repository walkthrough. Never a quality/production claim."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from tests.factories import Factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    args = parser.parse_args()
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    actor, project, episode = uuid4(), uuid4(), uuid4()
    with db.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=actor)
        query(tx, "INSERT INTO episodes VALUES(:p,:e)", p=project, e=episode)
    repo = Repository(db, FileObjectStore(args.objects), actor, purpose="evaluation")
    producer = CodeProducer(
        kind="code", component="SYNTHETIC-ENGINEERING-DEMO", build_id="wp01-v1", operation_id=uuid4()
    )
    f = Factory(repo, project, episode, producer).setup()
    source, blob = f.source()
    note = f.note()
    dossier = f.dossier(note)
    plan = f.plan(dossier)
    script = f.script(dossier, plan)
    assert repo.get(project, script.ref) == script
    assert repo.read_blob(project, source.ref, blob)
    with db.transaction() as tx:
        rows = query(
            tx,
            "SELECT * FROM artifact_revisions WHERE project_id=:p ORDER BY created_at,revision_id",
            p=project,
        ).mappings()
        artifacts = [repo._envelope(row).model_dump(mode="json") for row in rows]
    result = dict(
        test_data=True,
        semantic_acceptance=False,
        production_ready=False,
        description="СИНТЕТИЧЕСКИЕ инженерные данные: проверка хранения, не экспертное досье и не полный выпуск.",
        project_id=str(project),
        episode_id=str(episode),
        actor_id=str(actor),
        script_ref=script.ref.model_dump(mode="json"),
        artifacts=artifacts,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
