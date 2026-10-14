def test_import_app():
    from app.main import create_app

    app = create_app()
    # FastAPI 0.143+ stores included routers as lazy _IncludedRouter entries,
    # so inspect the public schema instead of relying on app.routes internals.
    paths = app.openapi()["paths"]
    assert "/health/live" in paths
    assert "/api/v1/work-orders" in paths
