"""Restore a saved corpus into an owned database; no original database or provider access."""

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
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pg-bin", required=True, type=Path)
    parser.add_argument(
        "--operation", choices=("replay", "artifact-diagnosis", "finalization-diagnosis"), default="replay"
    )
    args = parser.parse_args()
    source = json.loads(args.state.read_text())
    pg = args.pg_bin
    root = Path(__file__).resolve().parents[2]
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="podgen-offline-replay-") as name:
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
                f"-k {socket} -p 55471 -h '' -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                [pg / "createdb", "-h", socket, "-p", "55471", "-U", "runner", "replay"], check=True
            )
            subprocess.run(
                [
                    pg / "pg_restore",
                    "-h",
                    socket,
                    "-p",
                    "55471",
                    "-U",
                    "runner",
                    "-d",
                    "replay",
                    "--no-owner",
                    args.dump,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            state = base / "state.json"
            state.write_text(
                json.dumps(
                    dict(
                        source,
                        objects=str(objects),
                        database_url=f"postgresql+psycopg://runner@/replay?host={socket}&port=55471",
                    )
                )
            )
            env = dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(root / "src"),
                PODGEN_REPLAY_STATE=str(state),
                PODGEN_REPLAY_TRACE=str(args.trace.resolve()),
                PODGEN_REPLAY_OUT=str(args.out.resolve()),
                PODGEN_DATABASE_URL=json.loads(state.read_text())["database_url"],
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True
            )
            subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).with_name(
                            "worker.py"
                            if args.operation == "replay"
                            else args.operation.replace("-", "_") + ".py"
                        )
                    ),
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
                    "55471",
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
