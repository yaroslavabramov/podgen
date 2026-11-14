"""Measure a complete episode in an isolated database with retained offline responses."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--dump", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--code-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pg-bin", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.state.read_text())
    pg = args.pg_bin
    root = args.code_root.resolve() / "backend"
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="podgen-episode-replay-") as name:
        base = Path(name)
        socket, data, objects = (base / part for part in ("socket", "data", "objects"))
        socket.mkdir()

        def clone(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        shutil.copytree(source["objects"], objects, copy_function=clone)
        subprocess.run(
            [pg / "initdb", "-D", data, "-U", "runner", "-A", "trust", "--no-locale", "-E", "UTF8"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                pg / "pg_ctl",
                "-D",
                data,
                "-l",
                base / "postgres.log",
                "-o",
                f"-k {socket} -p 55479 -h '' -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                [pg / "createdb", "-h", socket, "-p", "55479", "-U", "runner", "replay"], check=True
            )
            subprocess.run(
                [
                    pg / "pg_restore",
                    "-h",
                    socket,
                    "-p",
                    "55479",
                    "-U",
                    "runner",
                    "-d",
                    "replay",
                    "--no-owner",
                    "--no-acl",
                    args.dump,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            state = args.out / "state.json"
            state.write_text(
                json.dumps(
                    dict(
                        source,
                        objects=str(objects),
                        database_url=f"postgresql+psycopg://runner@/replay?host={socket}&port=55479",
                    )
                )
            )
            env = dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(root / "src"),
                PODGEN_EPISODE_REPLAY_FIXTURES=str(args.fixtures.resolve()),
                PODGEN_EPISODE_REPLAY_EXPORT=str(args.export.resolve()),
                PODGEN_EPISODE_REPLAY_OUT=str(args.out),
                PODGEN_DATABASE_URL=json.loads(state.read_text())["database_url"],
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True
            )
            subprocess.run(
                [
                    pg / "psql",
                    "-h",
                    socket,
                    "-p",
                    "55479",
                    "-U",
                    "runner",
                    "-d",
                    "replay",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("worker.py")),
                ],
                cwd=root,
                env=env,
                check=True,
            )
            subprocess.run(
                [
                    pg / "pg_dump",
                    "-h",
                    socket,
                    "-p",
                    "55479",
                    "-U",
                    "runner",
                    "-d",
                    "replay",
                    "-Fc",
                    "-f",
                    args.out / "completed.dump",
                ],
                check=True,
            )
            shutil.copytree(objects, args.out / "objects", copy_function=clone)
        finally:
            subprocess.run(
                [pg / "pg_ctl", "-D", data, "stop", "-m", "fast"], check=True, stdout=subprocess.DEVNULL
            )


if __name__ == "__main__":
    main()
