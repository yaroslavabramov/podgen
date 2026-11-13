"""Measure a local model/tool cycle on an isolated copy; never call a live provider."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help="Saved state: database_url, objects, project, actor, run_id.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--pg-bin",
        type=Path,
        help="Directory containing pg_dump, initdb, pg_ctl, createdb and pg_restore.",
    )
    parser.add_argument(
        "--breakdown",
        action="store_true",
        help="Collect per-phase SQL counts; adds profiling overhead.",
    )
    args = parser.parse_args()
    source = json.loads(args.state.read_text())
    repo = Path(__file__).resolve().parents[3]
    backend = repo / "backend"
    pg = args.pg_bin
    if pg is None:
        pg = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    source_url = make_url(source["database_url"])
    source_env = dict(os.environ)
    if source_url.password:
        source_env["PGPASSWORD"] = source_url.password
    with tempfile.TemporaryDirectory(prefix="podgen-operation-") as temporary:
        owned = Path(temporary)
        socket, data, objects, dump = (
            owned / part for part in ("socket", "data", "objects", "source.dump")
        )
        socket.mkdir()
        subprocess.run(
            [
                pg / "pg_dump",
                "-h",
                source_url.query.get("host") or source_url.host or "localhost",
                "-p",
                str(source_url.port or source_url.query.get("port") or 5432),
                "-U",
                source_url.username or "postgres",
                "-d",
                source_url.database or "postgres",
                "-Fc",
                "-f",
                dump,
            ],
            env=source_env,
            check=True,
        )

        # Immutable inputs can be hardlinked safely; object publication never overwrites them.
        def clone_file(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        shutil.copytree(source["objects"], objects, copy_function=clone_file)
        subprocess.run(
            [
                pg / "initdb",
                "-D",
                data,
                "-U",
                "runner",
                "-A",
                "trust",
                "--no-locale",
                "-E",
                "UTF8",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                pg / "pg_ctl",
                "-D",
                data,
                "-l",
                owned / "postgres.log",
                "-o",
                f"-k {shlex.quote(str(socket))} -p 55465 -h ''",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                [pg / "createdb", "-h", socket, "-p", "55465", "-U", "runner", "probe"],
                check=True,
            )
            subprocess.run(
                [
                    pg / "pg_restore",
                    "-h",
                    socket,
                    "-p",
                    "55465",
                    "-U",
                    "runner",
                    "-d",
                    "probe",
                    "--no-owner",
                    dump,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            database_url = (
                f"postgresql+psycopg://runner@/probe?host={socket}&port=55465"
            )
            state = owned / "state.json"
            state.write_text(
                json.dumps(
                    dict(source, database_url=database_url, objects=str(objects))
                )
            )
            env = dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(backend / "src") + os.pathsep + str(backend),
                PODGEN_DATABASE_URL=database_url,
                PODGEN_TEST_DATABASE_URL=database_url,
                PODGEN_PROBE_STATE=str(state),
                PODGEN_PROBE_OBJECTS=str(objects),
                PODGEN_PROBE_REPORT=str(args.out.resolve()),
            )
            if args.breakdown:
                env["PODGEN_PROBE_BREAKDOWN"] = "1"
            else:
                env.pop("PODGEN_PROBE_BREAKDOWN", None)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=backend,
                env=env,
                check=True,
            )
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("operation_worker.py"))],
                cwd=backend,
                env=env,
                check=True,
            )
        finally:
            subprocess.run(
                [pg / "pg_ctl", "-D", data, "stop", "-m", "fast"],
                check=True,
                stdout=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    main()
