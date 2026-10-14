"""Тесты Excel-импорта справочников (app/routers/import_excel.py).

Честная стратегия окружения: живой Postgres из тестового окружения недоступен,
поэтому парсер тестируется как чистая функция, HTTP-слой — через TestClient
с фейковым пулом (паттерн tests/test_admin_api.py). Интеграционный тест
против живой БД помечен skip с причиной.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import deps
from app.routers import import_excel
from app.routers.import_excel import parse_import_workbook

EQ_HEADER = ["name", "inventory_no", "area_id", "type", "criticality"]
MAT_HEADER = ["name", "unit"]


def make_xlsx(header: list[str], rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


AREA = str(uuid.uuid4())


def eq_row(inv: str = "INV-1", **kw: Any) -> list[Any]:
    base: dict[str, Any] = {
        "name": "Насос",
        "inventory_no": inv,
        "area_id": AREA,
        "type": "pump",
        "criticality": "A",
    }
    base.update(kw)
    return [base[c] for c in EQ_HEADER]


# ---------- pure parser tests ----------


def test_parse_equipment_ok() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1"), eq_row("INV-2", criticality="B")])
    rows, errors = parse_import_workbook(data, "equipment")
    assert errors == []
    assert [r["inventory_no"] for r in rows] == ["INV-1", "INV-2"]


def test_parse_materials_ok() -> None:
    data = make_xlsx(MAT_HEADER, [["Фильтр", "шт"], ["Масло", "л"]])
    rows, errors = parse_import_workbook(data, "materials")
    assert errors == []
    assert [r["name"] for r in rows] == ["Фильтр", "Масло"]


def test_parse_missing_required_header_column() -> None:
    data = make_xlsx(["name", "type"], [["x", "pump"]])
    rows, errors = parse_import_workbook(data, "equipment")
    assert rows == []
    missing = {e["path"] for e in errors}
    assert {"inventory_no", "area_id", "criticality"} <= missing
    assert all(e["row"] == 1 for e in errors)


def test_parse_empty_required_cell_reports_excel_row_number() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1"), eq_row("INV-2", name="   ")])
    rows, errors = parse_import_workbook(data, "equipment")
    assert len(rows) == 1
    assert len(errors) == 1
    assert errors[0]["row"] == 3
    assert errors[0]["path"] == "name"


def test_parse_duplicate_inventory_no_inside_file() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("DUP"), eq_row("DUP")])
    rows, errors = parse_import_workbook(data, "equipment")
    assert len(rows) == 1
    assert len(errors) == 1
    assert errors[0]["row"] == 3
    assert errors[0]["path"] == "inventory_no"


def test_parse_duplicate_material_name_inside_file() -> None:
    data = make_xlsx(MAT_HEADER, [["Фильтр", "шт"], ["Фильтр", "кг"]])
    rows, errors = parse_import_workbook(data, "materials")
    assert len(rows) == 1
    assert errors[0]["row"] == 3
    assert errors[0]["path"] == "name"


def test_parse_invalid_criticality_and_unit() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("INV-9", criticality="Z")])
    _, errors = parse_import_workbook(data, "equipment")
    assert errors[0]["row"] == 2 and errors[0]["path"] == "criticality"

    data_m = make_xlsx(MAT_HEADER, [["Фильтр", "тонны"]])
    _, errors_m = parse_import_workbook(data_m, "materials")
    assert errors_m[0]["row"] == 2 and errors_m[0]["path"] == "unit"


def test_parse_invalid_area_id_and_usage() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("INV-9", area_id="not-a-uuid")])
    _, errors = parse_import_workbook(data, "equipment")
    assert errors[0]["row"] == 2 and errors[0]["path"] == "area_id"

    data_m = make_xlsx(["name", "unit", "typical_usage_per_order"], [["Ф", "шт", "много"]])
    _, errors_m = parse_import_workbook(data_m, "materials")
    assert errors_m[0]["row"] == 2 and errors_m[0]["path"] == "typical_usage_per_order"


def test_parse_invalid_file_and_unknown_ctype() -> None:
    with pytest.raises(ValueError):
        parse_import_workbook(b"not-an-xlsx", "equipment")
    with pytest.raises(ValueError):
        parse_import_workbook(b"", "materials")
    with pytest.raises(ValueError):
        parse_import_workbook(make_xlsx(MAT_HEADER, []), "areas")


def test_parse_skips_empty_rows_and_strips_cells() -> None:
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1", name="  Насос  "), [None] * 5])
    rows, errors = parse_import_workbook(data, "equipment")
    assert errors == []
    assert len(rows) == 1
    assert rows[0]["name"] == "Насос"


# ---------- HTTP layer with fake pool ----------


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    """Минимальный дубль asyncpg-пула с acquire()/transaction()."""

    def __init__(self, existing_keys: tuple[str, ...] = ()):
        self.existing = set(existing_keys)
        self.acquire_calls = 0
        self.tx_used = False
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> Any:
        pool = self

        class _Acq:
            async def __aenter__(self) -> Any:
                pool.acquire_calls += 1
                return pool.Conn(pool)

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Acq()

    class Conn:
        def __init__(self, pool: FakePool):
            self._pool = pool

        def transaction(self) -> _Tx:
            self._pool.tx_used = True
            return _Tx()

        async def fetchrow(self, query: str, *args: Any) -> Any:
            self._pool.fetchrow_calls.append((query, args))
            if args and args[0] in self._pool.existing:
                return {"id": str(uuid.uuid4())}
            return None

        async def execute(self, query: str, *args: Any) -> str:
            self._pool.execute_calls.append((query, args))
            return "OK"


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    clients: list[TestClient] = []

    def create(pool: FakePool, role: str = "admin") -> TestClient:
        async def get_pool() -> FakePool:
            return pool

        monkeypatch.setattr(import_excel, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": []}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def post_file(client: TestClient, ctype: str, data: bytes, dry_run: bool) -> Any:
    return client.post(
        f"/api/v1/dict/{ctype}/import",
        files={"file": ("imp.xlsx", data, XLSX_MIME)},
        params={"dry_run": str(dry_run).lower()},
    )


def test_http_dry_run_preview_writes_nothing(make_client: Any) -> None:
    pool = FakePool()
    client = make_client(pool)
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1")])
    resp = post_file(client, "equipment", data, True)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1 and body["errors"] == []
    assert pool.acquire_calls == 0
    assert pool.execute_calls == []


def test_http_bad_row_blocks_apply_and_reports_row(make_client: Any) -> None:
    pool = FakePool()
    client = make_client(pool)
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1"), eq_row("INV-1")])
    preview = post_file(client, "equipment", data, True)
    assert preview.status_code == 200
    assert preview.json()["errors"][0]["row"] == 3

    resp = post_file(client, "equipment", data, False)
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert any(d.get("row") == 3 for d in details)
    # Атомарность: раз валидация не прошла — в БД не пошли вообще.
    assert pool.acquire_calls == 0
    assert pool.execute_calls == []


def test_http_apply_counts_created_updated_in_one_transaction(make_client: Any) -> None:
    pool = FakePool(existing_keys=("INV-OLD",))
    client = make_client(pool)
    data = make_xlsx(EQ_HEADER, [eq_row("INV-NEW"), eq_row("INV-OLD")])
    resp = post_file(client, "equipment", data, False)
    assert resp.status_code == 200
    assert resp.json() == {"created": 1, "updated": 1}
    assert pool.acquire_calls == 1
    assert pool.tx_used is True
    kinds = [q for q, _ in pool.execute_calls]
    assert any(k.startswith("INSERT INTO equipment") for k in kinds)
    assert any(k.startswith("UPDATE equipment") for k in kinds)


def test_http_apply_materials_created(make_client: Any) -> None:
    pool = FakePool()
    client = make_client(pool)
    data = make_xlsx(MAT_HEADER, [["Фильтр", "шт"]])
    resp = post_file(client, "materials", data, False)
    assert resp.status_code == 200
    assert resp.json() == {"created": 1, "updated": 0}
    assert pool.execute_calls[0][0].startswith("INSERT INTO materials")


def test_http_forbidden_for_non_admin_and_404_for_unknown(make_client: Any) -> None:
    pool = FakePool()
    worker = make_client(pool, role="worker")
    data = make_xlsx(EQ_HEADER, [eq_row("INV-1")])
    assert post_file(worker, "equipment", data, True).status_code == 403

    admin_client = make_client(pool)
    assert post_file(admin_client, "areas", data, True).status_code == 404
    bad = post_file(admin_client, "equipment", b"junk-bytes", False)
    assert bad.status_code == 422


@pytest.mark.skip(
    reason="Нет живого Postgres-пула в тестовом окружении: "
    "атомарность на реальной БД (failed-apply не меняет count) "
    "проверяется только фейковым пулом выше"
)
def test_integration_atomicity_on_live_db() -> None:
    raise AssertionError("unreachable: skipped")
