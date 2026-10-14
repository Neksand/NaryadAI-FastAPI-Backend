"""Reference catalog tests for area scoping and nested member serialization."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.routers import catalog


class Pool:
    def __init__(self):
        self.rows = []
        self.site = None
        self.team = None
        self.equipment = None
        self.members = []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "FROM employees WHERE crew_id" in query:
            return self.members
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "FROM areas" in query:
            return self.site
        if "FROM crews" in query:
            return self.team
        if "FROM equipment" in query:
            return self.equipment
        return None


@pytest.fixture
def api(monkeypatch):
    clients = []

    def create(pool=None, role="manager", **fields):
        pool = pool or Pool()

        async def get_pool():
            return pool

        monkeypatch.setattr(catalog, "get_pool", get_pool)
        from app.main import create_app

        app = create_app()
        user = {"id": str(uuid.uuid4()), "role": role, "areaIds": [], **fields}
        app.dependency_overrides[deps.get_current_user] = lambda: user
        client = TestClient(app)
        clients.append(client)
        return client, pool

    yield create
    for client in clients:
        client.close()


def test_site_list_scopes_masters_and_serializes_uuid(api):
    area_id = uuid.uuid4()
    pool = Pool()
    pool.rows = [{"id": area_id, "name": "North", "name_kk": "Солтүстік", "code": "N"}]
    master = api(pool, role="master", areaIds=[str(area_id)])[0]
    assert master.get("/api/v1/sites").json()["data"][0]["id"] == str(area_id)
    assert "ANY($1::uuid[])" in pool.calls[-1][0]
    assert pool.calls[-1][1] == ([str(area_id)],)

    api(pool, role="manager")[0].get("/api/v1/sites")
    assert "ANY($1::uuid[])" not in pool.calls[-1][0]


def test_site_and_team_details_return_not_found_or_nested_member_rows(api):
    site_id, team_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool = Pool()
    client, _ = api(pool)
    assert client.get(f"/api/v1/sites/{site_id}").status_code == 404
    pool.site = {"id": site_id, "name": "North", "name_kk": None, "code": "N"}
    assert client.get(f"/api/v1/sites/{site_id}").json()["data"]["id"] == str(site_id)
    assert client.get(f"/api/v1/teams/{team_id}").status_code == 404
    pool.team = {"id": team_id, "name": "Repair A", "name_kk": "Жөндеу А", "area_id": site_id}
    pool.members = [
        {
            "id": member_id,
            "full_name": "A Worker",
            "specialty": "mechanic",
            "current_status": "free",
        }
    ]
    result = client.get(f"/api/v1/teams/{team_id}")
    assert result.status_code == 200
    assert result.json()["data"]["members"][0]["id"] == str(member_id)


def test_teams_query_preserves_optional_area_filter(api):
    client, pool = api()
    area_id = uuid.uuid4()
    assert client.get("/api/v1/teams", params={"area_id": str(area_id)}).status_code == 200
    assert pool.calls[-1][1] == (str(area_id),)
    assert client.get("/api/v1/teams").status_code == 200
    assert pool.calls[-1][1] == (None,)


def test_equipment_master_scope_and_explicit_area_filter_are_safe(api):
    area_id, equipment_id = uuid.uuid4(), uuid.uuid4()
    pool = Pool()
    pool.rows = [{"id": equipment_id, "area_id": area_id, "name": "Pump"}]
    client, _ = api(pool, role="master", areaIds=[str(area_id)])
    response = client.get("/api/v1/equipment", params={"q": "pump", "limit": 10})
    assert response.json()["data"][0]["id"] == str(equipment_id)
    query, args = pool.calls[-1]
    assert "area_id = ANY($1::uuid[])" in query
    assert args == ([str(area_id)], "pump", 10)

    client.get("/api/v1/equipment", params={"area_id": str(area_id), "q": "pump"})
    query, args = pool.calls[-1]
    assert "area_id=$1::uuid" in query and args == (str(area_id), "pump", 200)
    assert client.get("/api/v1/equipment", params={"limit": 0}).status_code == 422


def test_equipment_material_and_fault_code_details_and_filters(api):
    equipment_id, material_id, fault_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool = Pool()
    client, _ = api(pool)
    assert client.get(f"/api/v1/equipment/{equipment_id}").status_code == 404
    pool.equipment = {"id": equipment_id, "name": "Pump"}
    assert client.get(f"/api/v1/equipment/{equipment_id}").json()["data"]["id"] == str(equipment_id)
    pool.rows = [
        {
            "id": material_id,
            "name": "Seal",
            "name_kk": "Тығыздағыш",
            "unit": "pcs",
            "typical_usage_per_order": 2,
        }
    ]
    materials = client.get("/api/v1/materials", params={"q": "seal", "limit": 8})
    assert materials.json()["data"][0]["id"] == str(material_id)
    assert pool.calls[-1][1] == ("seal", 8)
    pool.rows = [
        {
            "id": fault_id,
            "code": "P-1",
            "fault_group": "Pump",
            "name": "Seal leak",
            "name_kk": None,
            "default_norm_minutes": 20,
        }
    ]
    codes = client.get("/api/v1/fault-codes", params={"q": "P-1"})
    assert codes.json()["data"][0]["id"] == str(fault_id)
    assert pool.calls[-1][1] == ("P-1",)
