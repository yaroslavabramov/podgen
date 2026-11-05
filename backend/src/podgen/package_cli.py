import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from podgen.domain.contracts.common import CodeProducer
from podgen.domain.packages import load_package, validate_package
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.packages import PackageImporter
from podgen.persistence.repository import Repository


def main():
    parser = argparse.ArgumentParser(
        description="Validate or transactionally import an expert artifact package"
    )
    parser.add_argument("command", choices=["validate", "import"])
    parser.add_argument("package", type=Path)
    parser.add_argument("--project", type=UUID)
    parser.add_argument("--episode", type=UUID)
    parser.add_argument("--actor", type=UUID)
    parser.add_argument("--objects", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    package = load_package(args.package)
    root = args.package.resolve().parent
    if args.command == "validate":
        print(json.dumps(validate_package(package, root), ensure_ascii=False, indent=2))
        return
    if any(value is None for value in (args.project, args.episode, args.actor, args.objects, args.out)):
        parser.error(
            "import requires --project --episode --actor --objects --out (an existing authorized scope)"
        )
    repo = Repository(
        Database(os.environ["PODGEN_DATABASE_URL"]),
        FileObjectStore(args.objects),
        args.actor,
        purpose="evaluation",
    )
    producer = CodeProducer(
        kind="code",
        component="expert-package-import",
        build_id="wp01-v1",
        operation_id=uuid5(package.package_id, "import"),
    )
    revisions = PackageImporter(repo).import_package(
        args.project, args.episode, package, root, producer=producer
    )
    args.out.write_text(
        json.dumps([r.model_dump(mode="json") for r in revisions], ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Imported {len(revisions)} exact immutable revisions; this does not confer semantic acceptance.")


if __name__ == "__main__":
    main()
