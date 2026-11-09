"""Explicit research-only access to historical writing configurations; ordinary writing uses production."""

from podgen.writing_cli import main as writing_main


def main(argv=None):
    return writing_main(argv, research=True)


if __name__ == "__main__":
    raise SystemExit(main())
