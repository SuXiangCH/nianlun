"""Execute the shipped nginx whitelist against an isolated API and SSE source."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.api_server.test_image_deployment import GATEWAY, PUBLIC_ROUTES, ROOT

from .conftest import (
    available_port,
    certificate,
    executable,
    running_process,
    wait_ready,
)


@pytest.fixture(params=[False, True], ids=["http", "tls"])
def gateway(request, tmp_path):
    @contextmanager
    def start(upstream_port):
        prefix = tmp_path / "nginx"
        prefix.mkdir()
        (prefix / "logs").mkdir()
        port = available_port()
        ca, key = certificate(prefix / "certs") if request.param else (None, None)
        listener = f"listen 127.0.0.1:{port} default_server"
        if ca:
            listener += (
                f' ssl;\n    ssl_certificate "{ca}";\n    ssl_certificate_key "{key}"'
            )
        source = GATEWAY.read_text()
        assert source.count("listen 8080 default_server") == 1
        assert source.count("http://127.0.0.1:8000") == 1
        source = source.replace("listen 8080 default_server", listener).replace(
            "http://127.0.0.1:8000", f"http://127.0.0.1:{upstream_port}"
        )
        included = prefix / "public-api.conf"
        included.write_text(source)
        config = prefix / "nginx.conf"
        config.write_text(
            "daemon off;\nmaster_process off;\npid logs/nginx.pid;\n"
            f'events {{}}\nhttp {{ include "{included}"; }}\n'
        )
        command = [executable("nginx"), "-p", str(prefix) + "/", "-c", str(config)]
        subprocess.run([*command, "-t"], check=True, capture_output=True, timeout=10)
        with running_process(command, prefix / "process.log") as process:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "nginx exited before listening"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            context = ssl.create_default_context(cafile=str(ca)) if ca else True
            with httpx.Client(
                base_url=f"{'https' if ca else 'http'}://127.0.0.1:{port}",
                verify=context,
                trust_env=False,
                timeout=10,
            ) as client:
                yield client

    return start


def test_real_whitelist_cors_upstream_failure_and_query_logs(gateway, tmp_path):
    upstream_port = available_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG"}
    }
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        PYTHONUNBUFFERED="1",
        NIANLUN_API_DATA_DIR=str(tmp_path / "data"),
        NIANLUN_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
        NIANLUN_API_DATABASE_PATH=str(tmp_path / "database.sqlite3"),
        NIANLUN_API_HOST="127.0.0.1",
        NIANLUN_API_PORT=str(upstream_port),
        NIANLUN_API_FTS_ENABLED="false",
        NIANLUN_API_VECTOR_ENABLED="false",
        NIANLUN_API_APPLICATION_ENABLED="false",
        NIANLUN_API_LOG_LEVEL="INFO",
    )
    query, bearer = "acceptance-fake-query-97ae", "acceptance-fake-bearer-13f8"
    with running_process(
        [sys.executable, "-m", "app.api_server.main"],
        tmp_path / "api.log",
        env=env,
        cwd=ROOT,
    ) as api:
        wait_ready(api, f"http://127.0.0.1:{upstream_port}/api/v1/healthz")
        with gateway(upstream_port) as client:
            for method, path in PUBLIC_ROUTES:
                response = client.request(
                    method,
                    path,
                    params={"token": query},
                    headers={"Authorization": "Bearer " + bearer},
                )
                # Actual application response proves nginx forwarded the exact route.
                assert response.headers["content-type"].startswith("application/json")
                assert response.headers.get("x-request-id")
                preflight = client.options(
                    path,
                    headers={
                        "Origin": "http://localhost:3000",
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": "authorization,x-nianlun-images",
                    },
                )
                assert preflight.status_code == 200
                assert (
                    preflight.headers["access-control-allow-origin"]
                    == "http://localhost:3000"
                )
                assert (
                    client.options(
                        path,
                        headers={
                            "Origin": "https://untrusted.example.test",
                            "Access-Control-Request-Method": method,
                        },
                    ).status_code
                    == 400
                )
                for rejected in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} - {
                    method
                }:
                    denied = client.request(rejected, path, params={"token": query})
                    assert denied.status_code == 404
                    assert "x-request-id" not in denied.headers
                for suffix in ("/", "/extra", ".json"):
                    assert client.request(method, path + suffix).status_code == 404
            for path in (
                "/api/v1/apps",
                "/api/v1/knowledge-bases",
                "/docs",
                "/openapi.json",
                "/internal/gateway/snapshot",
                "/v1/models/not-a-uuid",
            ):
                for method in ("GET", "POST", "OPTIONS"):
                    assert client.request(method, path).status_code == 404
            channel = socket.create_connection(
                ("127.0.0.1", client.base_url.port), timeout=2
            )
            if client.base_url.scheme == "https":
                context = ssl.create_default_context(
                    cafile=str(tmp_path / "nginx/certs/public.crt")
                )
                channel = context.wrap_socket(channel, server_hostname="127.0.0.1")
            with channel:
                channel.sendall(
                    f"GET /v1/models?token={query} HTTP/1.1\r\nHost: localhost\r\n"
                    f"Invalid Header: {bearer}\r\nConnection: close\r\n\r\n".encode()
                )
                assert b" 400 " in channel.recv(4096).split(b"\r\n", 1)[0]
            api.terminate()
            api.wait(timeout=10)
            failure = client.get(PUBLIC_ROUTES[2][1], params={"token": query})
            assert failure.status_code == 502
    for log in tmp_path.rglob("*.log"):
        text = log.read_text()
        assert query not in text
        assert bearer not in text
        assert "?token=" not in text
    assert "request.completed" in (tmp_path / "api.log").read_text()


def test_unbuffered_large_sse_and_location_headers(gateway):
    release = threading.Event()
    prefix = b'data: {"delta":"prefix"}\n\n'
    final = (
        b'data: {"delta":"data:image/png;base64,' + b"A" * (2 * 1024 * 1024) + b'"}\n\n'
    )

    class StreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Location", "/api/v1/runtime/apps/test/runs/test")
            self.send_header("Access-Control-Expose-Headers", "Location, X-Request-Id")
            self.end_headers()
            self.wfile.write(prefix)
            self.wfile.flush()
            if release.wait(10):
                self.wfile.write(final)
                self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with gateway(server.server_port) as client:
            with client.stream("POST", "/v1/chat/completions", json={}) as response:
                assert response.status_code == 200
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["location"].startswith("/api/v1/runtime/")
                assert "Location" in response.headers["access-control-expose-headers"]
                chunks = response.iter_bytes()
                received = next(chunks)
                assert received == prefix
                assert not release.is_set()
                release.set()
                assert received + b"".join(chunks) == prefix + final
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
