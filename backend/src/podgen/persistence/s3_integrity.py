"""S3 SHA256 receipts. Composite checksums retain their actual part boundaries."""

import base64
import hashlib
import io

from podgen.domain.errors import ContractError

from .blob_staging import BLOCK_SIZE

MIN_PART = 5 * 1024 * 1024
MAX_PART = 5 * 1024**3
DEFAULT_PART = 16 * 1024 * 1024
MAX_PARTS = 10000
MAX_OBJECT = MAX_PART * MAX_PARTS


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def full_checksum(staged):
    return encoded(bytes.fromhex(staged.sha256))


def check_receipt(response, expected):
    if response.get("ChecksumSHA256") != expected:
        raise ContractError("blob_integrity_error")


def part_size(length):
    if length > MAX_OBJECT:
        raise ContractError("blob_s3_size_limit")
    return max(DEFAULT_PART, (length + MAX_PARTS - 1) // MAX_PARTS)


def checksums(staged, size):
    if not MIN_PART <= size <= MAX_PART or (staged.byte_length + size - 1) // size > MAX_PARTS:
        raise ContractError("blob_integrity_error")
    parts, full = [], hashlib.sha256()
    with staged.path.open("rb") as source:
        remaining = staged.byte_length
        while remaining:
            digest, length = hashlib.sha256(), min(remaining, size)
            pending = length
            while pending:
                chunk = source.read(min(BLOCK_SIZE, pending))
                if not chunk:
                    raise ContractError("blob_integrity_error")
                digest.update(chunk)
                full.update(chunk)
                pending -= len(chunk)
            parts.append((length, digest.digest()))
            remaining -= length
        if source.read(1) or full.hexdigest() != staged.sha256:
            raise ContractError("blob_integrity_error")
    composite = encoded(hashlib.sha256(b"".join(d for _, d in parts)).digest()) + f"-{len(parts)}"
    return parts, composite


class FileRange(io.RawIOBase):
    """A seekable upload body; SDK length/retry probes cannot cross a part boundary."""

    def __init__(self, path, start, length):
        self.source = path.open("rb")
        self.start, self.length, self.position = start, length, 0
        self.source.seek(start)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.length + offset
        if whence not in (0, 1, 2) or not 0 <= position <= self.length:
            raise ValueError("invalid_part_seek")
        self.source.seek(self.start + position)
        self.position = position
        return position

    def read(self, size=-1):
        remaining = self.length - self.position
        data = self.source.read(remaining if size < 0 else min(size, remaining))
        self.position += len(data)
        return data

    def close(self):
        self.source.close()
        super().close()
