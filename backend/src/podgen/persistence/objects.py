"""Immutable, project-scoped stores. Storage keys never confer authorization."""

from .blob_staging import verify as verify
from .file_objects import FileObjectStore as FileObjectStore
from .s3_objects import S3ObjectStore as S3ObjectStore
