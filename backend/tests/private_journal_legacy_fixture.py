"""Build synthetic legacy evidence with a pinned historical writer.

Standalone utility, not a pytest fixture. Run against an explicitly supplied,
disposable database already migrated by that checkout. It neither starts nor
migrates PostgreSQL and never contacts a provider. Keep its JSON/object output
beside a local database dump; do not commit binary dumps.
"""

import argparse
import base64
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

WRITERS = {"v1": "45d0ee0c", "v2": "60aec210"}
HISTORY_TABLES = (
    "runtime_records",
    "evaluation_model_invocations",
    "evaluation_model_turns",
    "evaluation_invocation_events",
    "execution_provider_calls",
    "execution_tool_results",
)


def build(args):
    checkout = args.source_checkout.resolve()
    git = ["git", "-C", str(checkout)]
    commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    if not commit.startswith(WRITERS[args.writer]):
        raise ValueError("source checkout is not the requested historical writer")
    subprocess.run(
        [*git, "diff", "--quiet", "HEAD", "--", "backend/src", "backend/migrations", "backend/tests"],
        check=True,
    )
    sys.path[:0] = [str(checkout / "backend/src"), str(checkout / "backend")]

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from podgen.domain.contracts.common import BlobRef
    from podgen.persistence.database import Database, query
    from podgen.persistence.objects import FileObjectStore
    from podgen.persistence.repository import Repository
    from podgen.runtime.models.contracts import WireReply
    from podgen.runtime.models.count_receipts import native_count
    from podgen.runtime.models.gateway import ModelGateway
    from podgen.runtime.models.research_context import ResearchContext
    from podgen.runtime.models.research_history import ResearchHistory
    from tests.test_model_step_publication import run, setup

    if (
        Path(inspect.getsourcefile(setup)).resolve()
        != (checkout / "backend/tests/test_model_step_publication.py").resolve()
    ):
        raise RuntimeError("historical test helper was not loaded from pinned checkout")
    if not Path(inspect.getsourcefile(ResearchContext)).resolve().is_relative_to(checkout / "backend/src"):
        raise RuntimeError("historical production writer was not loaded from pinned checkout")
    database = Database(args.database_url)
    try:
        config = Config(str(checkout / "backend/alembic.ini"))
        config.set_main_option("script_location", str(checkout / "backend/migrations"))
        expected = set(ScriptDirectory.from_config(config).get_heads())
        with database.read_transaction() as tx:
            actual = set(query(tx, "SELECT version_num FROM alembic_version").scalars())
        if actual != expected:
            raise RuntimeError("database must be migrated by the pinned historical checkout first")

        repo = Repository(database, FileObjectStore(args.objects.resolve()), uuid4(), purpose="evaluation")
        project, episode = uuid4(), uuid4()
        with database.transaction() as tx:
            query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
            query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
            query(tx, "INSERT INTO episodes VALUES(:p,:e)", p=project, e=episode)
        journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(
            repo, (project, episode)
        )
        profile.provider = "openai"
        profile.api_family = "responses"
        profile.adapter_version = "openai-responses-v1"
        profile.model_revision_or_alias = snapshot[0]["model"]
        native = json.dumps(native_count(snapshot[0], "openai"), ensure_ascii=False).encode()
        raw_count = '{\n  "input_tokens": 100, "synthetic_fixture": "Полная квитанция 🛰️"\n}\n'.encode()
        calls = dict(count=0, submit=0)

        class LostAcknowledgement(Exception):
            pass

        def count(body):
            calls["count"] += 1
            return WireReply(raw_count, request_body=native)

        state = args.state

        def submit(*submitted, **kwargs):
            calls["submit"] += 1
            if args.state == "unknown":
                raise LostAcknowledgement("SYNTHETIC acknowledgement lost after submit intent")
            body = submitted[0]
            if state == "captured_tool":
                output = [
                    dict(
                        type="function_call",
                        call_id="synthetic-read",
                        name="read_source",
                        arguments='{"source_id":"S1"}',
                    )
                ]
            else:
                text = json.dumps(
                    dict(
                        research_requests=[
                            dict(
                                question="Which observation supports the account?",
                                why_needed="Read the original evidence.",
                            )
                        ]
                    )
                )
                output = [
                    dict(type="message", role="assistant", content=[dict(type="output_text", text=text)])
                ]
            return WireReply(
                json.dumps(
                    dict(
                        id="resp_synthetic_legacy",
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=output,
                        usage=dict(input_tokens=100, output_tokens=10),
                    )
                ).encode()
            )

        provider = SimpleNamespace(count=count, submit=submit)
        context = ResearchContext(
            ModelGateway(journal, lambda _: provider),
            root,
            profile,
            snapshot[0],
            ResearchHistory(journal, root),
            lambda: None,
        )
        assert context.measured(snapshot[0], 0, "normal") == 100
        if args.writer == "v2":
            context.pending_phase.commit()
        receipt = context.receipt(snapshot[0])
        assert receipt.validate(journal, snapshot[0], profile) == 100
        if args.state == "counted":
            phase.transition("counted", input_tokens=100)
            phase.commit()
        else:
            try:
                run(
                    ModelGateway(tj, lambda _: provider),
                    turn,
                    profile,
                    limits,
                    prepared_phase=phase,
                    request_snapshot=snapshot,
                    manifest_snapshot=manifest,
                    count_receipt=receipt,
                )
            except LostAcknowledgement:
                pass
            else:
                if args.state == "unknown":
                    raise AssertionError("unknown-submit fixture did not reach the historical submit path")
        assert calls == dict(count=1, submit=int(args.state != "counted"))

        with database.read_transaction() as tx:
            history = {
                table: list(
                    query(
                        tx,
                        f"SELECT row_to_json(t) FROM {table} t WHERE project_id=:p "
                        "ORDER BY row_to_json(t)::text",
                        p=project,
                    ).scalars()
                )
                for table in HISTORY_TABLES
                if query(tx, "SELECT to_regclass(:table)", table=table).scalar_one() is not None
            }
            blobs = list(query(tx, "SELECT * FROM blobs WHERE project_id=:p", p=project).mappings())
        wires = {}
        for row in blobs:
            ref = BlobRef.model_validate(
                {key: row[key] for key in ("storage_key", "sha256", "byte_length", "media_type")}
            )
            wires[ref.storage_key] = base64.b64encode(repo.objects.read(project, ref)).decode("ascii")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                dict(
                    contract="synthetic-legacy-journal-fixture/1",
                    writer=args.writer,
                    source_commit=commit,
                    state=args.state,
                    project_id=str(project),
                    actor_id=str(repo.actor_id),
                    run_id=str(root["run_id"]),
                    root_invocation_id=str(root["invocation_id"]),
                    physical_invocation_id=str(turn["invocation_id"]),
                    count_ref=receipt.reference.model_dump(mode="json"),
                    profile=vars(profile),
                    provider_calls=calls,
                    legacy_rows=history,
                    object_bytes_base64=wires,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writer", choices=WRITERS, required=True)
    parser.add_argument(
        "--state", choices=("counted", "unknown", "captured_tool", "captured_structured"), required=True
    )
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args())
