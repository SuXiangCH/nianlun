from types import SimpleNamespace

from fastapi import Response
from fastapi.testclient import TestClient

from app.api_server.config import ApiServerSettings
from app.api_server.main import create_app


def test_image_client_preflight_and_location_are_browser_accessible(tmp_path):
    settings = ApiServerSettings(data_dir=tmp_path, fts_enabled=False)
    app = create_app(settings, services=SimpleNamespace())

    @app.post("/test-location")
    def location(response: Response):
        response.headers["Location"] = "/api/v1/runtime/apps/app/runs/run"
        return {"ok": True}

    client = TestClient(app)
    origin = settings.cors_origins[0]
    preflight = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,idempotency-key,x-nianlun-include-images,x-nianlun-image-transport",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    response = client.post("/test-location", headers={"Origin": origin})
    assert response.status_code == 200
    exposed = {
        name.strip().lower()
        for name in response.headers.get("Access-Control-Expose-Headers", "").split(",")
    }
    assert {"location", "x-request-id"} <= exposed
    assert response.headers["Location"].endswith("/runs/run")
