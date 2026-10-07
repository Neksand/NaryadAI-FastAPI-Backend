import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()

ISSUER = "naryadai-api"
AUDIENCE = "naryadai-clients"


def hash_pin(pin: str) -> str:
    return _ph.hash(pin)


def verify_pin(hash_: str, pin: str) -> bool:
    try:
        return _ph.verify(hash_, pin)
    except (VerifyMismatchError, Exception):
        return False


def create_access_token(user: dict, session_id: str) -> str:
    from app.config import settings

    now = datetime.now(timezone.utc)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": user["id"],
        "sid": session_id,
        "role": user["role"],
        "area_ids": user.get("areaIds", []),
        "crew_id": user.get("crewId"),
        "name": user.get("fullName", ""),
        "authv": user.get("authVersion", 0),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, settings.ACCESS_TOKEN_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    from app.config import settings

    return jwt.decode(
        token, settings.ACCESS_TOKEN_SECRET, algorithms=["HS256"],
        issuer=ISSUER, audience=AUDIENCE,
    )


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()
