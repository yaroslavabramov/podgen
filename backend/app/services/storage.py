import io
import uuid

from minio import Minio

from app.config import settings


def get_minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_bucket(client: Minio) -> None:
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)


def upload_file(client: Minio, file_data: bytes, object_name: str, content_type: str = "application/octet-stream") -> str:
    ensure_bucket(client)
    client.put_object(
        settings.minio_bucket,
        object_name,
        io.BytesIO(file_data),
        length=len(file_data),
        content_type=content_type,
    )
    return object_name


def upload_text(client: Minio, text: str, object_name: str) -> str:
    data = text.encode("utf-8")
    return upload_file(client, data, object_name, "text/plain; charset=utf-8")


def download_text(client: Minio, object_name: str) -> str:
    response = client.get_object(settings.minio_bucket, object_name)
    try:
        return response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()


def download_file(client: Minio, object_name: str) -> bytes:
    response = client.get_object(settings.minio_bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def get_presigned_url(client: Minio, object_name: str, expires_hours: int = 1) -> str:
    from datetime import timedelta
    return client.presigned_get_object(settings.minio_bucket, object_name, expires=timedelta(hours=expires_hours))


def generate_object_name(prefix: str, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    unique = uuid.uuid4().hex[:12]
    return f"{prefix}/{unique}.{ext}" if ext else f"{prefix}/{unique}"
