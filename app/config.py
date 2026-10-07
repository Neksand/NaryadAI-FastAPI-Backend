from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    NODE_ENV: str = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "info"
    DATABASE_URL: str
    REDIS_URL: str
    ACCESS_TOKEN_SECRET: str
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_DAYS: int = 30
    CORS_ORIGINS: str = ""
    S3_ENDPOINT: str
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    S3_FORCE_PATH_STYLE: str = "false"
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    SEED_ADMIN_LOGIN: str = "admin"
    SEED_ADMIN_PIN: str = "1234"
    PDF_FONT_PATH: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def s3_force_path_style(self) -> bool:
        return self.S3_FORCE_PATH_STYLE.lower() == "true"


def load_settings() -> "Settings":
    s = Settings()  # type: ignore[call-arg]
    if len(s.ACCESS_TOKEN_SECRET) < 32:
        raise RuntimeError("ACCESS_TOKEN_SECRET must be at least 32 chars")
    if not (60 <= s.ACCESS_TOKEN_TTL_SECONDS <= 3600):
        raise RuntimeError("ACCESS_TOKEN_TTL_SECONDS must be 60..3600")
    if not (1 <= s.REFRESH_TOKEN_TTL_DAYS <= 90):
        raise RuntimeError("REFRESH_TOKEN_TTL_DAYS must be 1..90")
    if not (1024 <= s.MAX_UPLOAD_BYTES <= 25 * 1024 * 1024):
        raise RuntimeError("MAX_UPLOAD_BYTES must be 1024..25MiB")
    import re

    if not re.fullmatch(r"\d{4,8}", s.SEED_ADMIN_PIN):
        raise RuntimeError("SEED_ADMIN_PIN must be 4-8 digits")
    if s.NODE_ENV == "production" and s.SEED_ADMIN_PIN == "1234":
        raise RuntimeError("Set a non-default SEED_ADMIN_PIN in production")
    return s


settings = load_settings()
