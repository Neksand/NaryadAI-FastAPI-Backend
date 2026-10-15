"""The live verifier must not mutate a remote service by accident."""

import pytest

from scripts.verify_live import validate_target


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8080", "http://localhost:8080/", "http://[::1]:8080"]
)
def test_local_demo_origins_allowed(url):
    assert validate_target(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.naryad-ai.kz",
        "http://192.168.1.10:8080",
        "http://localhost.example.com",
        "http://127.0.0.1@api.example.com",
        "http://localhost/api/v1",
        "http://localhost?token=x",
        "http://user:password@localhost",
        "file:///tmp/app",
        "http://localhost#fragment",
    ],
)
def test_unsafe_or_nonlocal_origins_rejected(url):
    with pytest.raises(ValueError):
        validate_target(url)


def test_explicit_remote_override_does_not_allow_credentials():
    assert validate_target("https://test.example.com", True) == "https://test.example.com"
    with pytest.raises(ValueError):
        validate_target("https://user:password@test.example.com", True)
