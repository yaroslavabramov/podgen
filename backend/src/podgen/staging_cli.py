"""Reproduce the real editorial package byte/text/order/ACL round-trip."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import Database, query
from podgen.persistence.editorial_staging import EditorialStaging
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--project", type=UUID)
    parser.add_argument("--actor", type=UUID)
    parser.add_argument(
        "--new-test-project",
        action="store_true",
        help="Provision an isolated technical test namespace; requires DB admin rights",
    )
    args = parser.parse_args()
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    if args.new_test_project:
        if args.project or args.actor:
            parser.error("Choose --new-test-project or existing --project/--actor")
        args.project, args.actor = uuid4(), uuid4()
        with db.transaction() as tx:
            query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=args.project)
            query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=args.project, a=args.actor)
    if not args.project or not args.actor:
        parser.error("Supply an existing authorized --project/--actor or --new-test-project")
    repo = Repository(db, FileObjectStore(args.objects), args.actor, purpose="evaluation")
    report = EditorialStaging(repo).import_directory(args.project, args.root)
    if EditorialStaging(repo).import_directory(args.project, args.root) != report:
        raise AssertionError("Staging retry is not identical")
    generation = Repository(db, repo.objects, args.actor, purpose="generation")
    for ref in report["author_inputs"].values():
        generation.get_runtime(args.project, RuntimeRef.model_validate(ref))
    try:
        generation.get_runtime(args.project, RuntimeRef.model_validate(report["evaluator_index"]))
    except AccessDenied:
        pass
    else:
        raise AssertionError("Evaluator index leaked to generation")
    for path, ref in report["file_refs"].items():
        if path.startswith("evaluator-only/"):
            try:
                generation.get_runtime(args.project, RuntimeRef.model_validate(ref))
            except AccessDenied:
                pass
            else:
                raise AssertionError("Gold or candidate scripts leaked to generation")
    report.update(
        project_id=str(args.project),
        actor_id=str(args.actor),
        checks=[
            "all bytes and full text round-trip",
            "all draft/final utterances in exact code-point order",
            "identical repeat without duplicate runtime records",
            "generation reads only author/source projections",
            "evaluator index and every evaluator-only file denied to generation",
        ],
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                report=str(args.out),
                package_hash=report["package_hash"],
                files=len(report["file_refs"]),
                scripts={path: len(view) for path, view in report["script_views"].items()},
                semantic_acceptance=False,
                production_ready=False,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
