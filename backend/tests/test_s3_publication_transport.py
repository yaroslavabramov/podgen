"""Actual boto3/urllib3 streaming against a local checksum-validating HTTP endpoint."""

import base64
import hashlib
import threading
import tracemalloc
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config

from podgen.persistence.objects import S3ObjectStore
from podgen.persistence.s3_integrity import FileRange


class GeneratedStream:
    def __init__(self, length):
        self.remaining = length

    def read(self, size):
        size = min(size, self.remaining)
        self.remaining -= size
        return b"x" * size


def transport_operation(monkeypatch, length=17 * 1024 * 1024):
    stats, sizes, parts, state = Counter(), Counter(), [], {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, status, raw=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            if not headers or "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def do_HEAD(self):
            stats["head"] += 1
            if not state.get("complete"):
                return self.respond(404)
            self.respond(
                200,
                headers={
                    "Content-Length": length,
                    "Content-Type": "application/octet-stream",
                    "x-amz-checksum-sha256": state["composite"],
                    "x-amz-checksum-type": "COMPOSITE",
                    "x-amz-meta-podgen-part-size": state["part_size"],
                },
            )

        def do_POST(self):
            query = urlsplit(self.path).query
            if query == "uploads":
                stats["create_multipart"] += 1
                assert self.headers["x-amz-checksum-algorithm"] == "SHA256"
                assert self.headers["x-amz-checksum-type"] == "COMPOSITE"
                state["part_size"] = self.headers["x-amz-meta-podgen-part-size"]
                self.respond(
                    200,
                    b"<InitiateMultipartUploadResult><UploadId>upload</UploadId></InitiateMultipartUploadResult>",
                )
                return
            stats["complete_multipart"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            assert self.headers["If-None-Match"] == "*"
            assert int(self.headers["x-amz-mp-object-size"]) == length
            state["composite"] = (
                base64.b64encode(hashlib.sha256(b"".join(parts)).digest()).decode() + f"-{len(parts)}"
            )
            state["complete"] = True
            self.respond(
                200,
                f"<CompleteMultipartUploadResult><ChecksumSHA256>{state['composite']}</ChecksumSHA256><ChecksumType>COMPOSITE</ChecksumType></CompleteMultipartUploadResult>".encode(),
            )

        def do_PUT(self):
            stats["upload_part"] += 1
            assert not self.headers.get("Transfer-Encoding")
            number = int(parse_qs(urlsplit(self.path).query)["partNumber"][0])
            assert number == len(parts) + 1
            h, remaining = hashlib.sha256(), int(self.headers["Content-Length"])
            stats["upload_bytes"] += remaining
            while remaining:
                raw = self.rfile.read(min(64 * 1024, remaining))
                assert raw
                h.update(raw)
                remaining -= len(raw)
            parts.append(h.digest())
            checksum = base64.b64encode(h.digest()).decode()
            assert self.headers["x-amz-checksum-sha256"] == checksum
            self.respond(200, headers={"x-amz-checksum-sha256": checksum, "ETag": f"opaque-{number}"})

    original = FileRange.read

    def measured_read(body, size=-1):
        sizes[size] += 1
        return original(body, size)

    monkeypatch.setattr(FileRange, "read", measured_read)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{server.server_port}",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 0}, s3={"addressing_style": "path"}),
    )
    try:
        store, project = S3ObjectStore(client, "bucket"), uuid4()
        tracemalloc.start()
        ref = store.put(project, GeneratedStream(length), "application/octet-stream")
        assert store.put(project, GeneratedStream(length), "application/octet-stream") == ref
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()
    return dict(stats), dict(sizes), peak


def test_native_boto3_multipart_streams_bounded_chunks_and_uses_conditional_complete(monkeypatch):
    stats, sizes, peak = transport_operation(monkeypatch)
    assert stats == dict(
        head=2, create_multipart=1, upload_part=2, upload_bytes=17 * 1024 * 1024, complete_multipart=1
    )
    assert -1 not in sizes and max(sizes) <= 1024 * 1024
    assert peak < 8 * 1024 * 1024
