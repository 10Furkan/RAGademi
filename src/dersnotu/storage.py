"""Private object storage with an atomic, disposable local file cache."""

from __future__ import annotations

import mimetypes
import os
import shutil
import threading
import uuid
from pathlib import Path, PurePosixPath

from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings


class StorageError(RuntimeError):
    """A safe error message that never includes storage credentials."""


class S3Files:
    def __init__(self, config: Settings, *, client=None):
        self.root = (config.cache_dir / "remote-files").resolve()
        self.bucket = config.s3_bucket
        self.max_bytes = config.s3_max_file_mb * 1024 * 1024
        self._locks = [threading.Lock() for _ in range(32)]
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3", endpoint_url=config.s3_endpoint, region_name=config.s3_region,
                aws_access_key_id=config.s3_access_key.get_secret_value(),
                aws_secret_access_key=config.s3_secret_key.get_secret_value(),
                config=Config(
                    signature_version="s3v4", s3={"addressing_style": "path"},
                    connect_timeout=10, read_timeout=60,
                    retries={"max_attempts": 2, "mode": "standard"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        self.client = client

    def local_path(self, key: str) -> Path:
        key = key.replace("\\", "/")
        parts = PurePosixPath(key).parts
        if not parts or key.startswith("/") or ":" in key or any(p in {".", ".."} for p in key.split("/")):
            raise ValueError("Invalid storage object key.")
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Storage object must stay inside the local cache.")
        return path

    def key(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def put(self, key: str, source: Path) -> None:
        target = self.local_path(key)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise StorageError("The file exceeds the cloud storage's configured file-size limit.")
        try:
            with source.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket, Key=key, Body=body, ContentLength=size,
                    ContentType=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                    CacheControl="private, no-store",
                )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Cloud upload failed. Check storage access and available quota.") from exc
        # Immutable keys make this local copy safe across concurrent readers.
        if source.resolve() != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temp)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)

    def fetch(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        key = self.key(path)
        with self._locks[hash(key) % len(self._locks)]:
            if path.is_file():
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    with temp.open("wb") as output:
                        shutil.copyfileobj(body, output, length=1024 * 1024)
                finally:
                    body.close()
                if temp.stat().st_size != response["ContentLength"]:
                    raise StorageError("Cloud download was incomplete. Try again.")
                os.replace(temp, path)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
                    raise StorageError("Cloud download failed. Check storage access and quota.") from exc
            except BotoCoreError as exc:
                raise StorageError("Cloud storage is temporarily unreachable. Try again.") from exc
            finally:
                temp.unlink(missing_ok=True)
        return path

    def delete(self, path: Path) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(path))
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Cloud file deletion failed. Try again.") from exc
        path.unlink(missing_ok=True)
