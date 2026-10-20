import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fakeredis.aioredis import FakeRedis

from app.channel_gateway.__main__ import _build_ownership
from app.channel_gateway import __main__ as gateway_main
from app.channel_gateway.config import GatewaySettings
from app.channel_gateway.ownership import LocalOwnershipManager
from app.channel_gateway.redis_ownership import RedisOwnershipManager
from app.channel_gateway.manager import ChannelManager
from tests.channel_gateway.test_manager import (
    FakeAdapterFactory,
    FakeConfig,
    FakeStatus,
    FakeWorkflow,
    make_channel,
)

TOKENS = {
    "NIANLUN_INTERNAL_TOKEN": "i" * 32,
    "NIANLUN_GATEWAY_CONTROL_TOKEN": "c" * 32,
}


def load(**overrides: str) -> GatewaySettings:
    return GatewaySettings.from_env({**TOKENS, **overrides})


def test_local_backend_does_not_create_a_redis_client():
    ownership, redis = _build_ownership(load())
    assert isinstance(ownership, LocalOwnershipManager)
    assert redis is None


def test_redis_socket_timeouts_stay_inside_the_lease_safety_window():
    settings = load(
        GATEWAY_OWNERSHIP_BACKEND="redis",
        REDIS_URL="redis://127.0.0.1:6379/0",
        GATEWAY_LEASE_TTL_MS="30000",
        GATEWAY_LEASE_SAFETY_MS="5000",
    )
    ownership, redis = _build_ownership(settings)
    try:
        assert isinstance(ownership, RedisOwnershipManager)
        kwargs = redis.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == pytest.approx(5.0)
        assert kwargs["socket_timeout"] == pytest.approx(5.0)
    finally:
        asyncio.run(redis.aclose())


def test_redis_socket_timeout_never_exceeds_the_lease_safety_budget():
    cases = (
        ("3000", "400", 0.4),
        ("20000", "12000", 4.0),
        ("30000", "5000", 5.0),
        ("3000", "2500", 0.25),
        ("100", "99", 0.0005),
    )
    for ttl_ms, safety_ms, expected in cases:
        settings = load(
            GATEWAY_OWNERSHIP_BACKEND="redis",
            REDIS_URL="redis://127.0.0.1:6379/0",
            GATEWAY_LEASE_TTL_MS=ttl_ms,
            GATEWAY_LEASE_SAFETY_MS=safety_ms,
        )
        _ownership, redis = _build_ownership(settings)
        try:
            timeout = redis.connection_pool.connection_kwargs["socket_timeout"]
            budget = (settings.lease_ttl_ms - settings.lease_safety_ms) / 1000
            assert timeout == pytest.approx(expected)
            assert timeout <= budget
        finally:
            asyncio.run(redis.aclose())


def test_redis_url_cannot_override_lease_socket_timeouts():
    settings = load(
        GATEWAY_OWNERSHIP_BACKEND="redis",
        REDIS_URL=(
            "redis://127.0.0.1:6379/0?socket_timeout=60&socket_connect_timeout=60"
        ),
    )
    _ownership, redis = _build_ownership(settings)
    try:
        kwargs = redis.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == pytest.approx(5.0)
        assert kwargs["socket_connect_timeout"] == pytest.approx(5.0)
    finally:
        asyncio.run(redis.aclose())


@pytest.mark.parametrize("override_port", [None, "4200"])
def test_main_loads_project_dotenv_without_overriding_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override_port: str | None
):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in TOKENS.items())
        + "\nGATEWAY_PORT=4100\n",
        encoding="utf-8",
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("GATEWAY_PORT=4300\n", encoding="utf-8")
    monkeypatch.chdir(other)
    monkeypatch.setattr(gateway_main, "PROJECT_ROOT", project)
    received: list[GatewaySettings] = []

    async def serve(settings: GatewaySettings) -> None:
        received.append(settings)

    monkeypatch.setattr(gateway_main, "_serve", serve)
    environment = {} if override_port is None else {"GATEWAY_PORT": override_port}
    with patch.dict(os.environ, environment, clear=True):
        gateway_main.main()
    assert len(received) == 1
    assert received[0].internal_token == TOKENS["NIANLUN_INTERNAL_TOKEN"]
    assert received[0].control_token == TOKENS["NIANLUN_GATEWAY_CONTROL_TOKEN"]
    assert received[0].port == (4200 if override_port else 4100)


def test_main_rejects_invalid_token_without_serving_or_logging_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(gateway_main, "PROJECT_ROOT", tmp_path)
    with patch.dict(
        os.environ,
        {**TOKENS, "NIANLUN_INTERNAL_TOKEN": "invalid-test-secret"},
        clear=True,
    ):
        with pytest.raises(SystemExit) as error:
            gateway_main.main()
    assert error.value.code == 2
    assert "NIANLUN_INTERNAL_TOKEN" in caplog.text
    assert "invalid-test-secret" not in caplog.text


def test_gateway_keeps_the_connection_across_narrow_safety_window_renewals():
    async def scenario() -> None:
        settings = load(GATEWAY_LEASE_TTL_MS="3000", GATEWAY_LEASE_SAFETY_MS="2500")
        redis = FakeRedis(decode_responses=True)
        renewed = asyncio.Event()

        class CountingOwnership(RedisOwnershipManager):
            count = 0

            async def renew(self, lease):
                result = await super().renew(lease)
                if result:
                    self.count += 1
                    if self.count >= 3:
                        renewed.set()
                return result

        ownership = CountingOwnership(
            redis,
            key_prefix="test",
            ttl_ms=settings.lease_ttl_ms,
            safety_ms=settings.lease_safety_ms,
            instance_id="test",
        )
        config = FakeConfig()
        config.channels = [make_channel()]
        adapters = FakeAdapterFactory()
        manager = ChannelManager(
            settings, config, FakeWorkflow(), FakeStatus(), ownership, adapters
        )
        try:
            await manager.start()
            await asyncio.wait_for(renewed.wait(), timeout=2)
            assert manager.status()["online"] == 1
            assert len(adapters.instances) == 1
            assert not adapters.instances[0].closed
        finally:
            await manager.stop()
            await redis.aclose()

    asyncio.run(scenario())


def test_multiple_channels_keep_renewing_under_redis_latency():
    class LatencyRedis(FakeRedis):
        async def eval(self, *args, **kwargs):
            await asyncio.sleep(0.09)
            return await super().eval(*args, **kwargs)

    async def scenario() -> None:
        settings = load(GATEWAY_LEASE_TTL_MS="1000", GATEWAY_LEASE_SAFETY_MS="500")
        redis = LatencyRedis(decode_responses=True)
        ownership = RedisOwnershipManager(
            redis, key_prefix="test", ttl_ms=1000, safety_ms=500, instance_id="test"
        )
        config = FakeConfig()
        config.channels = [
            make_channel(channel_id=f"ch{i}", bot_id=f"bot{i}") for i in range(4)
        ]
        adapters = FakeAdapterFactory()
        manager = ChannelManager(
            settings, config, FakeWorkflow(), FakeStatus(), ownership, adapters
        )
        try:
            await manager.start()
            await asyncio.sleep(0.85)
            assert manager.status()["online"] == 4
            assert len(adapters.instances) == 4
            assert not any(adapter.closed for adapter in adapters.instances)
        finally:
            await manager.stop()
            await redis.aclose()

    asyncio.run(scenario())


def test_hung_renewal_does_not_delay_other_channels_and_is_cancelled_on_removal():
    async def scenario() -> None:
        settings = load(GATEWAY_LEASE_TTL_MS="1000", GATEWAY_LEASE_SAFETY_MS="500")
        redis = FakeRedis(decode_responses=True)
        healthy_renewed = asyncio.Event()
        blocked_cancelled = asyncio.Event()

        class BlockingOwnership(RedisOwnershipManager):
            healthy_renewals = 0

            async def renew(self, lease):
                if lease.channel_id == "slow":
                    try:
                        await asyncio.Event().wait()
                    finally:
                        blocked_cancelled.set()
                result = await super().renew(lease)
                if result:
                    self.healthy_renewals += 1
                    if self.healthy_renewals >= 3:
                        healthy_renewed.set()
                return result

        ownership = BlockingOwnership(
            redis, key_prefix="test", ttl_ms=1000, safety_ms=500, instance_id="test"
        )
        config = FakeConfig()
        config.channels = [
            make_channel(channel_id="slow", bot_id="slow-bot"),
            make_channel(channel_id="healthy", bot_id="healthy-bot"),
        ]
        adapters = FakeAdapterFactory()
        manager = ChannelManager(
            settings, config, FakeWorkflow(), FakeStatus(), ownership, adapters
        )
        try:
            await manager.start()
            healthy_task = manager._entries["healthy"].renew_task
            await asyncio.wait_for(healthy_renewed.wait(), timeout=2)
            await asyncio.wait_for(blocked_cancelled.wait(), timeout=1)
            assert set(manager._entries) == {"healthy"}
            assert manager.status()["online"] == 1
            assert adapters.instances[0].closed
            assert not adapters.instances[1].closed
            await manager.stop()
            assert healthy_task.done()
        finally:
            await manager.stop()
            await redis.aclose()

    asyncio.run(scenario())
