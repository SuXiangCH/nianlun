"""Opt-in infrastructure acceptance; never uses an existing service or bucket."""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


@pytest.fixture(scope="session", autouse=True)
def image_infrastructure():
    if os.environ.get("NIANLUN_RUN_IMAGE_INFRA_ACCEPTANCE") != "1":
        pytest.skip("Explicit image infrastructure acceptance is disabled")


def executable(name: str) -> str:
    configured = os.environ.get(f"NIANLUN_ACCEPTANCE_{name.upper()}_BIN", name)
    found = shutil.which(configured)
    if found is None:
        pytest.fail(f"Infrastructure acceptance requires a {name} executable")
    return found


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def certificate(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    config = root / "certificate.cnf"
    config.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n"
        "[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\n"
    )
    cert, key = root / "public.crt", root / "private.key"
    subprocess.run(
        [
            executable("openssl"),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return cert, key


def wait_ready(process: subprocess.Popen, url: str, *, ca: Path | None = None):
    context = ssl.create_default_context(cafile=str(ca)) if ca else None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert process.poll() is None, "Isolated acceptance process exited at startup"
        try:
            with urlopen(url, context=context, timeout=0.5):
                return
        except URLError:
            time.sleep(0.05)
    pytest.fail("Isolated acceptance process did not become ready")


@contextmanager
def running_process(command: list[str], log: Path, *, env=None, cwd=None):
    with log.open("w") as output:
        process = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, env=env, cwd=cwd
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
