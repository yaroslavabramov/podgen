"""Marker subprocess entrypoint used by PDF ingestion timeout wrapper."""

import argparse
import sys
from pathlib import Path


def _run(input_path: str, output_path: str) -> None:
    # Import lazily to keep CLI startup light and reuse marker setup logic.
    from podgen.sources.extraction.pdf import _marker_sync

    markdown = _marker_sync(input_path)
    Path(output_path).write_text(markdown, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run marker conversion in a standalone subprocess")
    parser.add_argument("--input", required=True, help="Input PDF file path")
    parser.add_argument("--output", required=True, help="Output markdown file path")
    args = parser.parse_args(argv)

    try:
        _run(args.input, args.output)
    except BaseException as exc:  # pragma: no cover - exercised from parent process
        print(repr(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
