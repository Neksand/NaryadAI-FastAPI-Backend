"""Admin and catalogue HTTP tests with a deterministic async pool double."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import admin


class Pool:
    def __init__(self, *, rows=None, row=None, links=None, settings_row=None, fail_area_link=False):
        self.rows = rows or []
        self.row = row
        self.links = links or []
        self.settings_row = settings_row
        self.fail_area_link = fail_area_link
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.links if "FROM employee_areas" in query else self.rows

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "FROM settings" in query:
            return self.settings_row
        return self.row or {"id": uuid.uuid4()}

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        if self.fail_area_link and "INSERT INTO employee_areas" in query:
            raise ValueError("invalid foreign key")


@pytest.fixture
def make_client(monkeypatch):
    clients = []

    def create(pool, role="admin", **user_fields):
        async def get_pool():
            return pool

        monkeypatch.setattr(admin, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], **user_fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def test_catalogue_reads_validate_type_and_scope_equipment_to_worker_areas(make_client):
    pool = Pool(rows=[{"id": uuid.uuid4(), "area_id": uuid.uuid4(), "name": "Pump"}])
    client = make_client(pool, role="worker", areaIds=["area-a"])

    assert client.get("/api/v1/dict/missing").status_code == 404
    response = client.get("/api/v1/dict/equipment", params={"q": "pump"})

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(pool.rows[0]["id"])
    query, args = pool.fetch_calls[0]
    assert "area_id = ANY($2::uuid[])" in query
    assert args == ("%pump%", ["area-a"], 500)


def test_catalogue_search_text_is_a_bound_value_and_catalogue_names_are_allowlisted(make_client):
    pool = Pool()
    client = make_client(pool)
    injection = "x%' OR 1=1; DROP TABLE employees;--"
    response = client.get("/api/v1/dict/areas", params={"q": injection})
    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert injection not in query
    assert args[0] == f"%{injection}%"

    invalid = client.get("/api/v1/dict/areas; DROP TABLE employees")
    assert invalid.status_code == 404
    assert len(pool.fetch_calls) == 1


SEARCH_ATTACKS = [
    "' OR 1=1 --",
    "%' OR 'x'='x",
    "'; DROP TABLE employees;--",
    "UNION SELECT login FROM employees",
    "pg_sleep(5)",
    "') OR ('a'='a",
    "\\' OR 1=1 --",
    "/* comment */",
    "; SELECT version()",
    "1; DELETE FROM work_orders",
    "%_\\",
    "x\x00 OR TRUE",
    '" OR "1"="1',
    "${jndi:ldap://attacker}",
    "{{7*7}}",
    "<script>alert(1)</script>",
    "0 OR 0=0",
    "name)--",
    "name/**/OR/**/true",
    "SELECT current_user",
    "information_schema.tables",
    "'; COMMIT;--",
    "-- trailing comment",
    "\\x27 UNION ALL SELECT NULL",
    "a' AND pg_catalog.pg_sleep(0.01)--",
    "LIKE '%' ESCAPE '\\'",
]


@pytest.mark.parametrize("search", SEARCH_ATTACKS)
def test_catalogue_search_payloads_remain_bound_values(make_client, search):
    pool = Pool()
    client = make_client(pool)
    response = client.get("/api/v1/dict/areas", params={"q": search})

    assert response.status_code == 200
    query, args = pool.fetch_calls[-1]
    assert search not in query
    assert args == (f"%{search}%", 500)


def test_employee_catalogue_rows_include_linked_area_ids(make_client):
    employee_id = uuid.uuid4()
    pool = Pool(
        rows=[{"id": employee_id, "login": "w1", "full_name": "Worker"}],
        links=[{"employee_id": employee_id, "area_ids": ["area-1", "area-2"]}],
    )
    response = make_client(pool, role="manager").get("/api/v1/dict/employees")

    assert response.status_code == 200
    assert response.json()["items"][0]["area_ids"] == ["area-1", "area-2"]
    assert "employee_areas" in pool.fetch_calls[1][0]


def test_catalogue_creation_enforces_admin_and_only_inserts_allowlisted_columns(make_client):
    pool = Pool()
    forbidden = make_client(pool, role="manager").post("/api/v1/dict/materials", json={"name": "x"})
    assert forbidden.status_code == 403

    client = make_client(pool)
    assert client.post("/api/v1/dict/unknown", json={"name": "x"}).status_code == 404
    assert client.post("/api/v1/dict/materials", json={}).status_code == 422
    response = client.post(
        "/api/v1/dict/materials", json={"name": "Filter", "unit": "pcs", "raw_sql": "DROP"}
    )

    assert response.status_code == 201
    query, args = pool.fetchrow_calls[-1]
    assert query.startswith("INSERT INTO materials(name,unit)")
    assert args == ("Filter", "pcs")


def test_employee_creation_validates_pin_and_creates_area_links(make_client):
    area_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    pool = Pool()
    client = make_client(pool)
    invalid = client.post("/api/v1/dict/employees", json={"login": "temp", "pin": "12"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["details"][0]["path"] == "pin"

    response = client.post(
        "/api/v1/dict/employees",
        json={
            "login": "temporary",
            "pin": "4782",
            "role": "worker",
            "area_ids": area_ids,
        },
    )

    assert response.status_code == 201
    assert response.json()["area_ids"] == area_ids
    insert, args = pool.fetchrow_calls[-1]
    assert "INSERT INTO employees" in insert
    assert args[0] == "temporary" and args[2] == "temporary"
    assert args[1] != "4782" and len(args[1]) > 30
    assert [call[1][1] for call in pool.execute_calls] == area_ids


def test_catalogue_patch_rejects_unsafe_inputs_and_updates_only_known_fields(make_client):
    pool = Pool()
    client = make_client(pool)
    item_id = str(uuid.uuid4())
    assert client.patch(f"/api/v1/dict/unknown/{item_id}", json={"name": "x"}).status_code == 404
    assert (
        client.patch(f"/api/v1/dict/employees/{item_id}", json={"pin": "1111"}).status_code == 422
    )
    assert (
        client.patch(f"/api/v1/dict/materials/{item_id}", json={"arbitrary": "x"}).status_code
        == 422
    )
    response = client.patch(
        f"/api/v1/dict/materials/{item_id}", json={"name": "Updated", "ignored": "x"}
    )

    assert response.status_code == 200
    query, args = pool.execute_calls[-1]
    assert "UPDATE materials SET name = $1 WHERE id=$2::uuid" in query
    assert args == ("Updated", item_id)


def test_employee_area_patch_validates_list_and_translates_missing_area(make_client):
    item_id = str(uuid.uuid4())
    pool = Pool(fail_area_link=True)
    client = make_client(pool)
    invalid_type = client.patch(f"/api/v1/dict/employees/{item_id}", json={"area_ids": "bad"})
    assert invalid_type.status_code == 422
    missing_area = client.patch(
        f"/api/v1/dict/employees/{item_id}", json={"area_ids": ["not-a-real-area"]}
    )
    assert missing_area.status_code == 422
    assert missing_area.json()["error"]["details"][0]["path"] == "area_ids"


def test_catalogue_delete_soft_deletes_only_known_type(make_client):
    pool = Pool()
    client = make_client(pool)
    item_id = str(uuid.uuid4())
    assert client.delete(f"/api/v1/dict/nope/{item_id}").status_code == 404
    response = client.delete(f"/api/v1/dict/areas/{item_id}")
    assert response.status_code == 200
    assert "UPDATE areas SET deleted_at=now()" in pool.execute_calls[-1][0]


def test_qr_lookup_hides_equipment_outside_master_area(make_client):
    row = {"id": uuid.uuid4(), "area_id": uuid.uuid4(), "qr_token": "qr-1"}
    pool = Pool(row=row)
    other_area = make_client(pool, role="master", areaIds=[str(uuid.uuid4())])
    assert other_area.get("/api/v1/equipment/by-qr/qr-1").status_code == 404
    matching_area = make_client(pool, role="master", areaIds=[str(row["area_id"])])
    result = matching_area.get("/api/v1/equipment/by-qr/qr-1")
    assert result.status_code == 200
    assert result.json()["area_id"] == str(row["area_id"])


@pytest.mark.parametrize(
    "stored",
    [None, {"value": '{"deadline_before_minutes":45}'}, {"value": {"deadline_repeat_minutes": 17}}],
)
def test_ai_settings_merge_defaults_for_missing_or_partial_values(make_client, stored):
    response = make_client(Pool(settings_row=stored), role="manager").get("/api/v1/settings/ai")
    assert response.status_code == 200
    result = response.json()
    assert result["deadline_before_minutes"] == (
        45
        if stored
        and stored["value"]
        and (
            stored["value"].get("deadline_before_minutes")
            if isinstance(stored["value"], dict)
            else 45
        )
        else 30
    )
    assert result["not_accepted_minutes"] == 10
    if stored and isinstance(stored["value"], dict):
        assert result["deadline_repeat_minutes"] == 17


def test_ai_settings_update_and_audit_are_admin_only(make_client):
    pool = Pool(rows=[{"action": "PIN_RESET"}])
    manager = make_client(pool, role="manager")
    assert (
        manager.put("/api/v1/settings/ai", json={"deadline_before_minutes": 12}).status_code == 403
    )
    admin_client = make_client(pool)
    payload = {
        "deadline_before_minutes": 12,
        "deadline_repeat_minutes": 21,
        "not_accepted_minutes": 6,
    }
    assert admin_client.put("/api/v1/settings/ai", json=payload).json() == payload
    assert pool.execute_calls[-1][1][0].find('"deadline_before_minutes": 12') >= 0
    audit = admin_client.get("/api/v1/audit?limit=1")
    assert audit.status_code == 200 and audit.json()["items"] == pool.rows
    assert pool.fetch_calls[-1][1][1] == 1


def test_reset_pin_and_unblock_enforce_pin_format_and_revoke_sessions(make_client):
    pool = Pool()
    client = make_client(pool)
    uid = str(uuid.uuid4())
    assert client.post(f"/api/v1/users/{uid}/reset-pin", json={"pin": "123"}).status_code == 422
    reset = client.post(f"/api/v1/users/{uid}/reset-pin", json={"pin": "582941"})
    assert reset.status_code == 200
    assert "auth_version=auth_version+1" in pool.execute_calls[-2][0]
    assert "UPDATE refresh_sessions SET revoked_at=now()" in pool.execute_calls[-1][0]
    assert client.post(f"/api/v1/users/{uid}/unblock").status_code == 200


@pytest.mark.parametrize(
    ("role", "where"),
    [
        ("worker", "assignee_id=$1::uuid"),
        ("master", "area_id = ANY($1::uuid[])"),
        ("manager", "due_at < now()"),
    ],
)
def test_alerts_query_scope_matches_current_role(make_client, role, where):
    pool = Pool(rows=[{"id": "order-1"}])
    response = make_client(pool, role=role, areaIds=["area-1"]).get("/api/v1/alerts?limit=3")
    assert response.status_code == 200
    assert response.json()["items"] == pool.rows
    assert where in pool.fetch_calls[-1][0]
