"""Immutable artifacts and durable, exclusive physical-call claims."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError("immutable_artifact_mismatch: " + str(path)) from None


def save(path, value):
    save_bytes(path, encoded(value))


def verified_response(directory):
    directory = Path(directory)
    assert read(directory / "submitted.json")["request_sha256"] == sha(
        (directory / "request.json").read_bytes()
    )
    raw = (directory / "response.bin").read_bytes()
    assert read(directory / "transport.json")["response_sha256"] == sha(raw)
    value = read(directory / "response.json")
    assert value == json.loads(raw)
    return value


def send_once(directory, body, transport):
    """No retries, including after timeout, crash, malformed reply or ambiguous failure."""
    directory = Path(directory)
    save(directory / "request.json", body)
    response_path = directory / "response.json"
    if response_path.exists():
        return verified_response(directory)
    claim = directory / "submitted.json"
    record = dict(
        client_request_id=str(uuid4()),
        request_sha256=sha((directory / "request.json").read_bytes()),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        with claim.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise RuntimeError(
            "Prior submission has no durable response; reconcile, never resubmit automatically."
        ) from None
    parent_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    status, request_id, raw = transport(body, record["client_request_id"])
    save_bytes(directory / "response.bin", raw)
    save(directory / "transport.json", dict(status=status, request_id=request_id, response_sha256=sha(raw)))
    value = json.loads(raw)
    save(response_path, value)
    return value
