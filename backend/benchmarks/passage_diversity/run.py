"""Restore a disposable corpus clone and replay saved searches with local ONNX only."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    for name in ("state", "dump", "review", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, default=Path("/opt/homebrew/opt/postgresql@17/bin"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="podgen-passage-replay-") as temporary:
        base = Path(temporary)
        data = base / "data"
        sock = base / "socket"
        sock.mkdir()
        pg = args.pg_bin
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
                f"-k {sock} -p 55474 -h ''",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                [pg / "createdb", "-h", sock, "-p", "55474", "-U", "runner", "passages"], check=True
            )
            subprocess.run(
                [
                    pg / "pg_restore",
                    "-h",
                    sock,
                    "-p",
                    "55474",
                    "-U",
                    "runner",
                    "--no-owner",
                    "--no-privileges",
                    "-d",
                    "passages",
                    args.dump,
                ],
                check=True,
            )
            url = f"postgresql+psycopg://runner@/passages?host={sock}&port=55474"
            env = dict(os.environ, PYTHONPATH=str(root / "backend/src"), PODGEN_DATABASE_URL=url)
            subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(root / "backend/alembic.ini"), "upgrade", "head"],
                cwd=root,
                env=env,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("worker.py")),
                    "--state",
                    str(args.state.resolve()),
                    "--review",
                    str(args.review.resolve()),
                    "--out",
                    str(args.out.resolve()),
                ],
                cwd=root,
                env=env,
                check=True,
            )
        finally:
            subprocess.run(
                [pg / "pg_ctl", "-D", data, "stop", "-m", "fast"], check=True, stdout=subprocess.DEVNULL
            )


if __name__ == "__main__":
    main()
