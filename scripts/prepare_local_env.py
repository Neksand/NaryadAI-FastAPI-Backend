"""Generate a fresh local-only .env; never replace existing configuration."""

import secrets
from pathlib import Path


def main() -> None:
    path = Path(".env")
    content = "\n".join(
        [
            "NODE_ENV=development",
            "DATABASE_URL=postgresql://naryadai@127.0.0.1:25432/naryadai_demo",
            "REDIS_URL=redis://127.0.0.1:26379/0",
            "ACCESS_TOKEN_SECRET=" + secrets.token_urlsafe(48),
            "CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:4173",
            "S3_ENDPOINT=http://127.0.0.1:29000",
            "S3_REGION=us-east-1",
            "S3_BUCKET=naryadai-local-demo",
            "S3_ACCESS_KEY=local-demo",
            "S3_SECRET_KEY=local-demo",
            "S3_FORCE_PATH_STYLE=true",
            "AI_MODE=mock",
            "AI_EXTERNAL_ENABLED=false",
            "AI_TEXT_PROVIDER=off",
            "AI_VISION_PROVIDER=off",
            "AI_AUDIO_PROVIDER=off",
            "SEED_ADMIN_LOGIN=admin",
            "SEED_ADMIN_PIN=1234",
            "",
        ]
    )
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write(content)
    print("Created local .env with a random token secret; value is not printed")


if __name__ == "__main__":
    main()
