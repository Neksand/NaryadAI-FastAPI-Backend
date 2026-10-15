import contextlib
from typing import Any

import boto3
from botocore.client import Config as BotoConfig

_s3 = None


def get_s3() -> Any:
    from app.config import settings

    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            config=BotoConfig(
                s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
                connect_timeout=8,
                read_timeout=15,
                retries={"max_attempts": 3},
            ),
        )
    return _s3


def ensure_upload_bucket() -> None:
    from app.config import settings

    try:
        s3 = get_s3()
        try:
            s3.head_bucket(Bucket=settings.S3_BUCKET)
        except Exception:
            with contextlib.suppress(Exception):
                s3.create_bucket(Bucket=settings.S3_BUCKET)
    except Exception:
        pass


PUT_MAX_ATTEMPTS = 3
PUT_BACKOFF_SECONDS = (0.5, 1.5, 3.0)


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    import time

    from botocore.exceptions import BotoCoreError, ClientError

    from app.config import settings

    last_error: Exception | None = None
    for attempt in range(PUT_MAX_ATTEMPTS):
        try:
            get_s3().put_object(
                Bucket=settings.S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
            return
        except (BotoCoreError, ClientError, ConnectionError, TimeoutError, OSError) as e:
            last_error = e
            # 4xx (кроме throttling/временных) не повторяем — это не сеть.
            if isinstance(e, ClientError):
                code = str(e.response.get("Error", {}).get("Code", ""))
                status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
                if (
                    status
                    and 400 <= status < 500
                    and "Throttl" not in code
                    and "Timeout" not in code
                ):
                    raise
            if attempt + 1 < PUT_MAX_ATTEMPTS:
                time.sleep(PUT_BACKOFF_SECONDS[min(attempt, len(PUT_BACKOFF_SECONDS) - 1)])
    assert last_error is not None
    raise last_error


def signed_url(key: str, expires_in: int) -> str:
    from app.config import settings

    return str(
        get_s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    )
