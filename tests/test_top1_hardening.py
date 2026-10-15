"""Top-1 hardening: pure unit gaps for config/deps/photos/notify/cursor.

No DB/Redis/S3. Covers missing branches from coverage.json:
app/config.py 83%, app/deps.py 85%, app/routers/photos.py 89%,
app/text_utils.py 90%, app/notifications.py templates.
"""

import uuid

import pytest

# --- config.load_settings validation (app/config.py:65-78) ---


def _settings_kwargs(**over):
    base = dict(
        DATABASE_URL="postgresql://localhost/db",
        REDIS_URL="redis://localhost/6379",
        ACCESS_TOKEN_SECRET="x" * 32,
        S3_ENDPOINT="http://localhost:9000",
        S3_BUCKET="b",
        S3_ACCESS_KEY="a",
        S3_SECRET_KEY="s",
    )
    base.update(over)
    return base


def _bad_settings(_cls=None, **over):
    from app.config import Settings as _S

    cls = _cls or _S
    return cls(**_settings_kwargs(**over))


def test_load_settings_rejects_short_secret(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(
        cfg, "Settings", lambda *a, **k: _bad_settings(orig, ACCESS_TOKEN_SECRET="short")
    )
    with pytest.raises(RuntimeError, match="ACCESS_TOKEN_SECRET"):
        cfg.load_settings()


def test_load_settings_rejects_bad_access_ttl(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(
        cfg, "Settings", lambda *a, **k: _bad_settings(orig, ACCESS_TOKEN_TTL_SECONDS=10)
    )
    with pytest.raises(RuntimeError, match="ACCESS_TOKEN_TTL_SECONDS"):
        cfg.load_settings()
    monkeypatch.setattr(
        cfg, "Settings", lambda *a, **k: _bad_settings(orig, ACCESS_TOKEN_TTL_SECONDS=9999)
    )
    with pytest.raises(RuntimeError, match="ACCESS_TOKEN_TTL_SECONDS"):
        cfg.load_settings()


def test_load_settings_rejects_bad_refresh_ttl(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(
        cfg, "Settings", lambda *a, **k: _bad_settings(orig, REFRESH_TOKEN_TTL_DAYS=0)
    )
    with pytest.raises(RuntimeError, match="REFRESH_TOKEN_TTL_DAYS"):
        cfg.load_settings()
    monkeypatch.setattr(
        cfg, "Settings", lambda *a, **k: _bad_settings(orig, REFRESH_TOKEN_TTL_DAYS=365)
    )
    with pytest.raises(RuntimeError, match="REFRESH_TOKEN_TTL_DAYS"):
        cfg.load_settings()


def test_load_settings_rejects_bad_upload_bytes(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(cfg, "Settings", lambda *a, **k: _bad_settings(orig, MAX_UPLOAD_BYTES=10))
    with pytest.raises(RuntimeError, match="MAX_UPLOAD_BYTES"):
        cfg.load_settings()


def test_load_settings_rejects_bad_admin_pin(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(cfg, "Settings", lambda *a, **k: _bad_settings(orig, SEED_ADMIN_PIN="abc"))
    with pytest.raises(RuntimeError, match="SEED_ADMIN_PIN"):
        cfg.load_settings()


def test_load_settings_rejects_default_pin_in_production(monkeypatch):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(
        cfg,
        "Settings",
        lambda *a, **k: _bad_settings(orig, NODE_ENV="production", SEED_ADMIN_PIN="1234"),
    )
    with pytest.raises(RuntimeError, match="non-default SEED_ADMIN_PIN"):
        cfg.load_settings()


def test_settings_cors_and_s3_helpers():
    from app.config import Settings

    s = Settings(
        **_settings_kwargs(
            CORS_ORIGINS="http://a.test, http://a.test ,,http://b.test", S3_FORCE_PATH_STYLE="True"
        )
    )
    origins = s.cors_origins
    assert "http://a.test" in origins and "http://b.test" in origins
    assert "tauri://localhost" in origins
    assert s.s3_force_path_style is True
    s2 = Settings(**_settings_kwargs(S3_FORCE_PATH_STYLE="false"))
    assert s2.s3_force_path_style is False


# --- deps.get_current_user unauthorized paths (no DB) ---


class _Q(dict):
    pass


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_get_current_user_no_token_raises():
    import asyncio

    from app.deps import get_current_user
    from app.errors import AppError

    with pytest.raises(AppError) as e:
        asyncio.run(get_current_user(_Req(headers={}, query={})))
    assert e.value.status_code == 401


def test_get_current_user_bad_token_raises():
    import asyncio

    from app.deps import get_current_user
    from app.errors import AppError

    with pytest.raises(AppError) as e:
        asyncio.run(get_current_user(_Req(headers={"authorization": "Bearer broken"}, query={})))
    assert e.value.status_code == 401


def test_get_current_user_bad_uuid_claims_raise():
    import asyncio

    import app.deps as deps
    from app.deps import get_current_user
    from app.errors import AppError

    orig = deps.decode_access_token
    try:
        deps.decode_access_token = lambda t: {
            "sub": "not-uuid",
            "sid": str(uuid.uuid4()),
            "role": "worker",
        }
        with pytest.raises(AppError) as e:
            asyncio.run(get_current_user(_Req(headers={"authorization": "Bearer x"})))
        assert e.value.status_code == 401
        deps.decode_access_token = lambda t: {
            "sub": str(uuid.uuid4()),
            "sid": "bad",
            "role": "worker",
        }
        with pytest.raises(AppError):
            asyncio.run(get_current_user(_Req(headers={"authorization": "Bearer x"})))
    finally:
        deps.decode_access_token = orig


def test_get_current_user_bad_role_raises():
    import asyncio

    import app.deps as deps
    from app.deps import get_current_user
    from app.errors import AppError

    orig = deps.decode_access_token
    try:
        deps.decode_access_token = lambda t: {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "role": "hacker",
        }
        with pytest.raises(AppError) as e:
            asyncio.run(get_current_user(_Req(headers={"authorization": "Bearer x"})))
        assert e.value.status_code == 401
    finally:
        deps.decode_access_token = orig


def test_get_current_user_query_token_fallback_missing_still_401():
    import asyncio

    from app.deps import get_current_user
    from app.errors import AppError

    with pytest.raises(AppError):
        asyncio.run(get_current_user(_Req(headers={}, query={"token": "bad"})))


def test_require_role_and_alias():
    from app.deps import require_any_role, require_role
    from app.errors import AppError

    require_role({"role": "master"}, "master", "admin")
    require_any_role({"role": "admin"}, "master", "admin")
    with pytest.raises(AppError) as e:
        require_role({"role": "worker"}, "master")
    assert e.value.status_code == 403
    with pytest.raises(AppError):
        require_any_role({"role": "worker"}, "admin")


def test_cursor_roundtrip_and_invalid():
    from app.deps import decode_cursor, encode_cursor
    from app.errors import AppError

    assert decode_cursor(None) is None
    oid = str(uuid.uuid4())
    cur = encode_cursor(oid, "2026-10-14T00:00:00+00:00")
    back = decode_cursor(cur)
    assert back["id"] == oid
    with pytest.raises(AppError):
        decode_cursor("!!!not-base64!!!")


def test_allowed_actions_master_and_forbidden():
    from app.deps import allowed_actions

    master = {"role": "master", "areaIds": ["a1"]}
    assert set(allowed_actions(master, {"status": "ai_review", "area_id": "a1"})) == {
        "close",
        "return_to_rework",
    }
    assert allowed_actions(master, {"status": "issued", "area_id": "other"}) == []
    assert allowed_actions({"role": "stranger"}, {"status": "issued"}) == []


# --- photos helpers ---


def test_photo_verify_accepts_and_rejects():
    import io

    from PIL import Image

    from app.errors import AppError
    from app.routers.photos import _verify

    for format_name, mime in (("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")):
        output = io.BytesIO()
        Image.new("RGB", (12, 12), (100, 20, 30)).save(output, format=format_name)
        _verify(output.getvalue(), mime)
    with pytest.raises(AppError):
        _verify(b"hello", "image/gif")
    with pytest.raises(AppError):
        _verify(b"not-a-jpeg", "image/jpeg")


def test_parse_client_taken_at_valid_and_invalid():
    from app.errors import AppError
    from app.routers.photos import _parse_client_taken_at

    assert _parse_client_taken_at(None) is None
    ok = _parse_client_taken_at("2026-10-01T10:00:00Z")
    assert ok is not None and ok.year == 2026
    ok2 = _parse_client_taken_at("2026-10-01T12:00:00+05:00")
    assert ok2 is not None
    with pytest.raises(AppError):
        _parse_client_taken_at("not-a-date")
    with pytest.raises(AppError):
        _parse_client_taken_at("2026-10-01T25:00:00Z")
    with pytest.raises(AppError):
        _parse_client_taken_at("2026-10-01T10:00:00+99:99")
    with pytest.raises(AppError):
        _parse_client_taken_at("1800-01-01T00:00:00Z")


def test_hamming_and_dhash_fallbacks():
    from app.routers.photos import _dhash, _exif_taken_at, hamming

    assert hamming("ff", "ff") == 0
    assert hamming("ff", "00") == 8
    assert hamming("zz", "00") == 64
    assert _dhash(b"not-an-image") is None
    assert _exif_taken_at(b"not-an-image") is None


def test_text_utils_non_string_passthrough():
    from app.text_utils import normalize_text_dict, trim_edges

    assert trim_edges(123) == 123  # type: ignore
    assert trim_edges(None) is None  # type: ignore
    out = normalize_text_dict({"description": 123, "comment": None})
    assert out["description"] == 123 and out["comment"] is None


def test_notification_templates_ru_kk_and_fallback():
    from app.notifications import TEMPLATES, _t

    assert _t("ru-text", "kk-text", "kk") == "kk-text"
    assert _t("ru-text", "kk-text", "ru") == "ru-text"
    for key, builder in TEMPLATES.items():
        ru_title, ru_msg = builder(
            {
                "number": 7,
                "reason": "тест",
                "score": 5,
                "verdict": "ok",
                "minutes_left": 3,
                "minutes_overdue": 4,
            },
            "ru",
        )
        kk_title, kk_msg = builder(
            {
                "number": 7,
                "reason": "тест",
                "score": 5,
                "verdict": "ok",
                "minutes_left": 3,
                "minutes_overdue": 4,
            },
            "kk",
        )
        assert ru_title and ru_msg and kk_title and kk_msg, key


def test_notifications_s_uuid_conversion():
    from app.routers.notifications import _s

    oid = uuid.uuid4()
    d = _s({"id": oid, "name": "x"})
    assert d["id"] == str(oid) and d["name"] == "x"


def test_errors_factory_codes():
    from app.errors import AppError, conflict, forbidden, not_found, unauthorized, validation_error

    assert unauthorized().status_code == 401
    assert forbidden().status_code == 403
    assert not_found().status_code == 404
    assert conflict("c", "m").code == "c"
    assert validation_error([{"path": "x"}]).status_code == 422
    e = AppError(400, "x", "m")
    assert e.translation_key == "error.x"


def test_security_pin_and_tokens():
    from app.security import (
        create_refresh_token,
        hash_pin,
        hash_refresh_token,
        sha256_hex,
        verify_pin,
    )

    h = hash_pin("1234")
    assert verify_pin(h, "1234") is True
    assert verify_pin(h, "0000") is False
    assert len(create_refresh_token()) > 20
    assert len(hash_refresh_token("abc")) == 64
    assert sha256_hex("a") == sha256_hex(b"a")


def test_idempotency_key_header_passthrough():
    from app.deps import idempotency_key_from_header

    assert idempotency_key_from_header("k1") == "k1"
    assert idempotency_key_from_header(None) is None


@pytest.mark.parametrize(
    "secret",
    [
        "x" * 40,
        "replace-with-a-secure-random-token-of-32-characters",
        "change-me-for-production-please-123456",
    ],
)
def test_production_rejects_long_but_placeholder_secrets(monkeypatch, secret):
    import app.config as cfg

    orig = cfg.Settings
    monkeypatch.setattr(
        cfg,
        "Settings",
        lambda *a, **k: _bad_settings(
            orig, NODE_ENV="production", SEED_ADMIN_PIN="958372", ACCESS_TOKEN_SECRET=secret
        ),
    )
    with pytest.raises(RuntimeError, match="non-placeholder ACCESS_TOKEN_SECRET"):
        cfg.load_settings()
