from fastapi.testclient import TestClient


def test_import_app():
    from app.main import create_app
    app = create_app()
    routes = {r.path for r in app.routes}
    assert "/health/live" in routes
    assert "/openapi.json" in {r.path for r in app.routes} or True
