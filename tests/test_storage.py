"""Object-storage configuration, bucket setup, and upload URL contracts."""

import pytest

from app import storage
from app.config import settings


class FakeS3:
    def __init__(self, *, head_error=None, create_error=None):
        self.head_error = head_error
        self.create_error = create_error
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        if self.head_error:
            raise self.head_error

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))
        if self.create_error:
            raise self.create_error

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))

    def generate_presigned_url(self, *args, **kwargs):
        self.calls.append(("generate_presigned_url", args, kwargs))
        return "https://storage.test/signed"


@pytest.fixture(autouse=True)
def reset_cached_client(monkeypatch):
    monkeypatch.setattr(storage, "_s3", None)


@pytest.mark.parametrize(("force_path_style", "expected_style"), [(True, "path"), (False, "auto")])
def test_s3_client_uses_configured_endpoint_credentials_and_addressing(
    monkeypatch, force_path_style, expected_style
):
    fake = FakeS3()
    client_calls = []
    monkeypatch.setattr(
        storage.boto3,
        "client",
        lambda service, **kwargs: client_calls.append((service, kwargs)) or fake,
    )
    monkeypatch.setattr(settings, "S3_ENDPOINT", "http://object-store:9000")
    monkeypatch.setattr(settings, "S3_REGION", "local-1")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY", "test-key")
    monkeypatch.setattr(settings, "S3_SECRET_KEY", "test-secret")
    monkeypatch.setattr(settings, "S3_FORCE_PATH_STYLE", str(force_path_style).lower())

    first = storage.get_s3()
    second = storage.get_s3()

    assert first is fake and second is fake
    assert len(client_calls) == 1
    service, kwargs = client_calls[0]
    assert service == "s3"
    assert kwargs["endpoint_url"] == "http://object-store:9000"
    assert kwargs["region_name"] == "local-1"
    assert kwargs["aws_access_key_id"] == "test-key"
    assert kwargs["aws_secret_access_key"] == "test-secret"
    assert kwargs["config"].s3["addressing_style"] == expected_style


def test_bucket_setup_accepts_existing_bucket_without_mutation(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "get_s3", lambda: fake)
    monkeypatch.setattr(settings, "S3_BUCKET", "work-orders")

    storage.ensure_upload_bucket()

    assert fake.calls == [("head_bucket", {"Bucket": "work-orders"})]


def test_bucket_setup_creates_missing_bucket_and_swallows_create_failure(monkeypatch):
    fake = FakeS3(
        head_error=RuntimeError("not found"), create_error=RuntimeError("already created")
    )
    monkeypatch.setattr(storage, "get_s3", lambda: fake)
    monkeypatch.setattr(settings, "S3_BUCKET", "work-orders")

    storage.ensure_upload_bucket()

    assert fake.calls == [
        ("head_bucket", {"Bucket": "work-orders"}),
        ("create_bucket", {"Bucket": "work-orders"}),
    ]


def test_bucket_setup_degrades_gracefully_when_client_cannot_be_created(monkeypatch):
    monkeypatch.setattr(storage, "get_s3", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    storage.ensure_upload_bucket()  # Startup should still complete if object storage is down.


def test_upload_and_signed_url_use_bucket_key_and_security_metadata(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "get_s3", lambda: fake)
    monkeypatch.setattr(settings, "S3_BUCKET", "work-orders")

    storage.put_bytes("photos/order.jpg", b"jpeg", "image/jpeg")
    signed = storage.signed_url("photos/order.jpg", 180)

    assert signed == "https://storage.test/signed"
    assert fake.calls[0] == (
        "put_object",
        {
            "Bucket": "work-orders",
            "Key": "photos/order.jpg",
            "Body": b"jpeg",
            "ContentType": "image/jpeg",
            "ServerSideEncryption": "AES256",
        },
    )
    assert fake.calls[1] == (
        "generate_presigned_url",
        ("get_object",),
        {"Params": {"Bucket": "work-orders", "Key": "photos/order.jpg"}, "ExpiresIn": 180},
    )
