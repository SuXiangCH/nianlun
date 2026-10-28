"""Deployment routing contracts and real-process query-log acceptance."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "deploy/application-api.nginx.conf"
APP = "a1b2c3d4-1234-5678-9abc-1234567890ab"
RUN = "a1b2c3d4-2234-5678-9abc-1234567890ab"
ATTACHMENT = "a1b2c3d4-3234-5678-9abc-1234567890ab"
GRANT = "a1b2c3d4-4234-5678-9abc-1234567890ab"


def gateway_rules() -> list[re.Pattern[str]]:
    """Execute the supplied map's regex routing subset without a daemon."""
    config = GATEWAY.read_text()
    match = re.search(
        r'map\s+"\$request_method:\$uri"\s+\$nianlun_public_api_allowed\s*\{(.*?)^\}',
        config,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "Gateway must map the exact method and normalized URI"
    lines = [line.split("#", 1)[0].strip() for line in match[1].splitlines()]
    lines = [line for line in lines if line]
    assert lines.pop(0) == "default 0;"
    patterns = []
    for line in lines:
        route = re.fullmatch(r'"~([^"]+)"\s+1;', line)
        assert route is not None, f"Unsupported gateway rule: {line}"
        patterns.append(re.compile(route[1]))
    assert re.search(
        r"if\s*\(\$nianlun_public_api_allowed = 0\)\s*\{\s*return 404;\s*\}", config
    )
    return patterns


PUBLIC_ROUTES = [
    ("POST", f"/api/v1/runtime/apps/{APP}/chat"),
    ("GET", f"/api/v1/runtime/apps/{APP}/runs/{RUN}"),
    ("GET", f"/api/v1/runtime/images/{GRANT}"),
    ("POST", f"/api/v1/runtime/apps/{APP}/runs/{RUN}/images/{ATTACHMENT}/access"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/v1/models"),
    ("GET", f"/v1/models/{APP}"),
]


@pytest.mark.parametrize("method,path", PUBLIC_ROUTES)
def test_gateway_routes_exact_public_methods_and_cors_preflight(method, path):
    rules = gateway_rules()
    assert any(rule.fullmatch(f"{method}:{path}") for rule in rules)
    assert any(rule.fullmatch(f"OPTIONS:{path}") for rule in rules)
    for rejected in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "TRACE"} - {
        method
    }:
        assert not any(rule.fullmatch(f"{rejected}:{path}") for rule in rules)
    for suffix in ("/", "/extra", ".json"):
        assert not any(rule.fullmatch(f"{method}:{path}{suffix}") for rule in rules)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/apps",
        f"/api/v1/apps/{APP}/runs",
        f"/api/v1/chat/{APP}",
        "/api/v1/models",
        "/api/v1/knowledge-bases",
        "/api/v1/upload",
        "/docs",
        "/openapi.json",
        "/internal/gateway/snapshot",
        "/v1/models/attachment_01",
        "/v1/models/not-a-uuid",
        "/v1/models/" + "a" * 36,
        "/api/v1/runtime/apps/app-1/chat",
        f"/api/v1/runtime/apps/{APP}/runs/run-1",
        "/api/v1/runtime/images/grant-1",
        f"/api/v1/runtime/apps/{APP}/runs/{RUN}/images/attachment_01/access",
    ],
)
def test_gateway_rejects_management_and_non_uuid_paths(path):
    rules = gateway_rules()
    for method in ("GET", "POST", "OPTIONS"):
        assert not any(rule.fullmatch(f"{method}:{path}") for rule in rules)


def test_gateway_suppresses_both_raw_access_and_request_bearing_error_logs():
    config = re.sub(r"#.*", "", GATEWAY.read_text())
    assert re.search(r"access_log\s+off;", config)
    assert re.search(r"error_log\s+/dev/null\s+crit;", config)
    assert "$request_uri" not in config
    assert "$args" not in config
    assert "$query_string" not in config


@pytest.mark.parametrize("entrypoint", ["module", "uvicorn_cli"])
def test_real_uvicorn_entrypoint_keeps_path_metrics_and_omits_query_tokens(
    tmp_path: Path, entrypoint: str
):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT"}
    }
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONUNBUFFERED": "1",
            "NIANLUN_API_DATA_DIR": str(tmp_path / "data"),
            "NIANLUN_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "NIANLUN_API_DATABASE_PATH": str(tmp_path / "database.sqlite3"),
            "NIANLUN_API_HOST": "127.0.0.1",
            "NIANLUN_API_PORT": str(port),
            "NIANLUN_API_FTS_ENABLED": "false",
            "NIANLUN_API_VECTOR_ENABLED": "false",
            "NIANLUN_API_APPLICATION_ENABLED": "false",
            "NIANLUN_API_LOG_LEVEL": "INFO",
        }
    )
    command = [sys.executable, "-m", "app.api_server.main"]
    if entrypoint == "uvicorn_cli":
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.api_server.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
    log_path = tmp_path / "server.log"
    token = "deployment_fake_query_secret_4a9f"
    bearer = "deployment_fake_bearer_secret_82c1"
    image_path = f"/api/v1/runtime/images/{GRANT}"
    base = f"http://127.0.0.1:{port}"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                assert process.poll() is None, log_path.read_text()
                try:
                    with urlopen(f"{base}/api/v1/healthz", timeout=0.5) as response:
                        assert response.status == 200
                    break
                except URLError:
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)
            request = Request(
                f"{base}{image_path}?token={token}",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "X-Request-Id": "deployment-log-check",
                },
            )
            with pytest.raises(HTTPError) as failure:
                urlopen(request, timeout=2)
            assert failure.value.code == 404
            failure.value.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    logs = log_path.read_text()
    assert token not in logs
    assert bearer not in logs
    assert "?token=" not in logs
    assert (
        f"request.started request_id=deployment-log-check method=GET path={image_path}"
        in logs
    )
    assert (
        f"request.completed request_id=deployment-log-check method=GET path={image_path} status_code=404 duration_ms="
        in logs
    )
    assert (tmp_path / "database.sqlite3").is_file()
