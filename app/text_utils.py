"""Нормализация человеческого ввода: трим только краёв.

Пароли, ID, ключи, файлы не трогаем. Нормализация выполняется до расчёта
idempotency-хэша, чтобы случайные пробелы по краям не создавали дубли.
"""

from typing import Any


def trim_edges(value: str) -> str:
    if not isinstance(value, str):
        return value
    return value.strip()


def normalize_text_dict(
    body: dict[str, Any], keys: tuple[str, ...] = ("description", "comment", "work_done_text")
) -> dict[str, Any]:
    out = dict(body)
    for k in keys:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = trim_edges(v)
    form = out.get("form")
    if isinstance(form, dict):
        out["form"] = normalize_text_dict(form, ("work_done_text", "comment"))
    return out
