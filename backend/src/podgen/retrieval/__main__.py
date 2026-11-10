"""Install or verify the pinned local retrieval models before running paid generation."""

import argparse
import json

from . import RetrievalUnavailable
from .assets import prepare, verify
from .config import ENCODER, RERANKER


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        for spec in (ENCODER, RERANKER):
            verify(spec)
    except (RetrievalUnavailable, OSError) as error:
        parser.exit(
            2, f"Source search preparation failed: {error}. Retry prepare; no source data is uploaded.\n"
        )
    print(json.dumps(dict(status="ready", models=[spec["repository"] for spec in (ENCODER, RERANKER)])))


if __name__ == "__main__":
    main()
