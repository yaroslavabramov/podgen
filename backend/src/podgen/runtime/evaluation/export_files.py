"""Atomic archive files and constant-memory JSON arrays, with unchanged JSON formatting."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def projection(invocation):
    return {
        key: str(value) if key.endswith("_id") and value is not None else value
        for key, value in invocation.items()
    }


@contextmanager
def json_array(path):
    """Match json.dumps(values, ensure_ascii=False, indent=2) + newline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            count = 0
            output.write("[")

            def append(value):
                nonlocal count
                output.write(",\n" if count else "\n")
                output.write(
                    "\n".join(
                        "  " + line for line in json.dumps(value, ensure_ascii=False, indent=2).split("\n")
                    )
                )
                count += 1

            yield append
            output.write("\n]\n" if count else "]\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
