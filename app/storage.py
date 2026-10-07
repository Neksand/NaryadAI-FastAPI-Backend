import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

_s3 = None


def get_s3():
    from app.config import settings

    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            config=BotoConfig(s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
                              connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}),
        )
    return _s3


def ensure_upload_bucket() -> None:
    from app.config import settings

    try:
        s3 = get_s3()
        try:
            s3.head_bucket(Bucket=settings.S3_BUCKET)
        except Exception:
            try:
                s3.create_bucket(Bucket=settings.S3_BUCKET)
            except Exception:
                pass
    except Exception:
        pass


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    from app.config import settings

    get_s3().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data,
                        ContentType=content_type, ServerSideEncryption="AES256")


def signed_url(key: str, expires_in: int) -> str:
    from app.config import settings

    return get_s3().generate_presigned_url(
        "get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=expires_in)
