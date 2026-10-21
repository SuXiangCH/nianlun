import asyncio
import os
from dataclasses import replace
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from app.channel_gateway.ownership import AcquireInput, RecoveryInput
from app.channel_gateway.protocol import GatewayError
from app.channel_gateway.redis_ownership import RedisOwnershipManager


def manager(
    redis: Any,
    instance_id: str = "gateway-1",
    *,
    ttl_ms: int = 30_000,
    safety_ms: int = 5_000,
    prefix: str = "test",
) -> RedisOwnershipManager:
    return RedisOwnershipManager(
        redis,
        key_prefix=prefix,
        ttl_ms=ttl_ms,
        safety_ms=safety_ms,
        instance_id=instance_id,
    )


def acquire_input(
    bot_id: str = "bot-1", channel_id: str = "channel-1", revision: int = 1
) -> AcquireInput:
    return AcquireInput(bot_id=bot_id, channel_id=channel_id, revision=revision)


def test_acquire_is_exclusive_and_release_allows_a_new_generation():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            first = manager(redis, "gateway-1")
            second = manager(redis, "gateway-2")
            lease = await first.acquire(acquire_input())
            assert lease is not None
            assert lease.generation == 1
            assert await second.acquire(acquire_input()) is None

            await first.release(lease)
            again = await second.acquire(acquire_input())
            assert again is not None
            assert again.generation == 2
            assert again.token != lease.token
            await second.release(again)
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_acquire_rejects_stale_binding_and_expired_revision():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            ownership = manager(redis)
            lease = await ownership.acquire(acquire_input(revision=2))
            assert lease is not None
            await ownership.release(lease)

            with pytest.raises(GatewayError) as error:
                await ownership.acquire(
                    acquire_input(channel_id="channel-2", revision=2)
                )
            assert error.value.code == "CHANNEL_BINDING_STALE"

            with pytest.raises(GatewayError) as error:
                await ownership.acquire(acquire_input(revision=1))
            assert error.value.code == "CHANNEL_REVISION_STALE"

            expired = await ownership.acquire(acquire_input(revision=3))
            assert expired is not None
            assert (
                await ownership.publish_status(expired, "offline", "CHANNEL_EXPIRED")
                is not None
            )
            await ownership.release(expired)

            with pytest.raises(GatewayError) as error:
                await ownership.acquire(acquire_input(revision=3))
            assert error.value.code == "CHANNEL_EXPIRED"

            recovered = await ownership.acquire(acquire_input(revision=4))
            assert recovered is not None
            await ownership.release(recovered)
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_block_prevents_reacquire_until_recovery():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            first = manager(redis, "gateway-1")
            second = manager(redis, "gateway-2")
            lease = await first.acquire(acquire_input())
            assert lease is not None
            assert await first.block(lease) is True
            await first.release(lease)

            with pytest.raises(GatewayError) as error:
                await second.acquire(acquire_input())
            assert error.value.code == "CHANNEL_CONNECTION_CONFLICT"

            recovery = RecoveryInput("bot-1", "channel-1", 1, "request-1")
            assert await first.recover(recovery) is True
            assert await first.has_recovered(lease) is True
            assert await first.renew(lease) is False
            assert await first.recover(recovery) is False

            new_lease = await second.acquire(acquire_input())
            assert new_lease is not None
            await second.release(new_lease)
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_recover_refuses_to_preempt_a_live_owner():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            ownership = manager(redis)
            lease = await ownership.acquire(acquire_input())
            assert lease is not None
            with pytest.raises(GatewayError) as error:
                await ownership.recover(
                    RecoveryInput("bot-1", "channel-1", 1, "request-1")
                )
            assert error.value.code == "CHANNEL_RECOVERY_PENDING"
            assert error.value.retryable is True
            await ownership.release(lease)
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_status_roundtrip_and_terminal_states_survive_release():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            first = manager(redis, "gateway-1")
            second = manager(redis, "gateway-2")

            lease = await first.acquire(acquire_input(revision=2))
            assert lease is not None
            online = await first.publish_status(lease, "online", None)
            assert online is not None
            assert online.status == "online"
            assert online.error_code is None
            assert await second.read_status(acquire_input(revision=2)) == online

            await first.release(lease)
            assert await second.read_status(acquire_input(revision=2)) is None

            expired = await first.acquire(acquire_input(revision=3))
            assert expired is not None
            terminal = await first.publish_status(expired, "offline", "CHANNEL_EXPIRED")
            assert terminal is not None
            await first.release(expired)
            assert await second.read_status(acquire_input(revision=3)) == terminal

            conflict = await first.acquire(acquire_input(bot_id="bot-2", revision=1))
            assert conflict is not None
            assert await first.block(conflict) is True
            conflict_state = await first.publish_status(
                conflict, "error", "CONNECTION_CONFLICT"
            )
            assert conflict_state is not None
            await first.release(conflict)
            assert (
                await second.read_status(acquire_input(bot_id="bot-2", revision=1))
                == conflict_state
            )
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_renew_validates_token_and_recovery_version():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            ownership = manager(redis)
            lease = await ownership.acquire(acquire_input())
            assert lease is not None
            assert await ownership.renew(lease) is True
            assert await ownership.renew(replace(lease, token="other")) is False

            assert await ownership.block(lease) is True
            await ownership.release(lease)
            assert (
                await ownership.recover(
                    RecoveryInput("bot-1", "channel-1", 1, "request-1")
                )
                is True
            )
            assert await ownership.renew(lease) is False
            assert lease.safe_until == 0
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_status_is_not_read_beyond_the_safety_window():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            ownership = manager(redis, ttl_ms=100, safety_ms=50)
            lease = await ownership.acquire(acquire_input())
            assert lease is not None
            assert await ownership.publish_status(lease, "online", None) is not None
            await asyncio.sleep(0.06)
            assert await ownership.read_status(acquire_input()) is None
            assert await ownership.publish_status(lease, "online", None) is None
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_keys_share_a_bot_hash_tag():
    async def scenario():
        redis = FakeRedis(decode_responses=True)
        try:
            ownership = manager(redis)
            lease = await ownership.acquire(acquire_input())
            assert lease is not None
            digest = sha256(b"bot-1").hexdigest()
            keys = {str(key) for key in await redis.keys("*")}
            assert f"test:{{{digest}}}:owner" in keys
            assert f"test:{{{digest}}}:generation" in keys
            assert f"test:{{{digest}}}:binding" in keys
            assert all(f"{{{digest}}}" in key for key in keys)
            await ownership.release(lease)
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_invalid_lease_configuration_is_rejected():
    with pytest.raises(ValueError):
        RedisOwnershipManager(
            None,
            key_prefix="test",
            ttl_ms=5_000,
            safety_ms=5_000,
            instance_id="gateway-1",
        )
    with pytest.raises(ValueError):
        RedisOwnershipManager(
            None,
            key_prefix="test",
            ttl_ms=30_000,
            safety_ms=0,
            instance_id="gateway-1",
        )


@pytest.mark.skipif(
    not os.getenv("REDIS_TEST_URL"), reason="REDIS_TEST_URL is not configured"
)
def test_real_redis_roundtrip():
    async def scenario():
        from redis.asyncio import Redis

        client = Redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
        prefix = f"nianlun-test-{uuid4().hex}"
        ownership = RedisOwnershipManager(
            client,
            key_prefix=prefix,
            ttl_ms=30_000,
            safety_ms=5_000,
            instance_id="integration",
        )
        try:
            lease = await ownership.acquire(
                acquire_input(bot_id="bot-it", channel_id="channel-it")
            )
            assert lease is not None
            state = await ownership.publish_status(lease, "online", None)
            assert state is not None
            assert (
                await ownership.read_status(
                    acquire_input(bot_id="bot-it", channel_id="channel-it")
                )
                == state
            )
            await ownership.release(lease)
        finally:
            keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
            if keys:
                await client.delete(*keys)
            await client.aclose()

    asyncio.run(scenario())
