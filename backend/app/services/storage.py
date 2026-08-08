from functools import lru_cache

from minio import Minio
from minio.commonconfig import CopySource

from ..config import get_settings


class ObjectStorage:
    def __init__(self, client, bucket_name):
        self.client = client
        self.bucket_name = bucket_name

    def bucket_exists(self):
        return self.client.bucket_exists(self.bucket_name)

    def ensure_bucket(self):
        if not self.bucket_exists():
            self.client.make_bucket(self.bucket_name)

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        return self.client.fput_object(
            self.bucket_name,
            object_name,
            str(file_path),
            content_type=content_type,
        )

    def iter_object(self, object_name, chunk_size=1024 * 1024):
        response = self.client.get_object(self.bucket_name, object_name)
        try:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()
            response.release_conn()

    def remove_object(self, object_name):
        self.client.remove_object(self.bucket_name, object_name)

    def copy_object(self, source_object_name, destination_object_name):
        return self.client.copy_object(
            self.bucket_name,
            destination_object_name,
            CopySource(self.bucket_name, source_object_name),
        )


@lru_cache
def get_object_storage():
    settings = get_settings()
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    return ObjectStorage(client, settings.minio_bucket)
