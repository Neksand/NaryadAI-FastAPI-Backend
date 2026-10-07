from typing import Any, Optional


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None, translation_key: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        # Machine-readable i18n key for the frontend (locales/ru.json, locales/kk.json).
        # DB/business logic always uses `code`; this never affects branching.
        self.translation_key = translation_key or f"error.{code}"


def unauthorized(msg: str = "Не авторизован") -> AppError:
    return AppError(401, "unauthorized", msg)


def forbidden(msg: str = "Доступ запрещён") -> AppError:
    return AppError(403, "forbidden", msg)


def not_found(msg: str = "Не найдено") -> AppError:
    return AppError(404, "not_found", msg)


def unauthorized(msg: str = "Не авторизован") -> AppError:
    return AppError(401, "unauthorized", msg)


def forbidden(msg: str = "Доступ запрещён") -> AppError:
    return AppError(403, "forbidden", msg)


def not_found(msg: str = "Не найдено") -> AppError:
    return AppError(404, "not_found", msg)


def conflict(code: str, msg: str, details: Any = None) -> AppError:
    return AppError(409, code, msg, details)


def validation_error(details: Any) -> AppError:
    return AppError(422, "validation_error", "Проверьте входные данные", details)
