"""Run the standalone gateway with ``python -m app.channel_gateway``."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from dotenv import load_dotenv

from app.channel_gateway.clients import ConfigClient, StatusClient, WorkflowClient
from app.channel_gateway.config import GatewayConfigurationError, GatewaySettings
from app.channel_gateway.manager import ChannelManager
from app.channel_gateway.ownership import LocalOwnershipManager
from app.channel_gateway.redis_ownership import RedisOwnershipManager
from app.channel_gateway.server import create_app
from app.channel_gateway.wecom_adapter import WecomAdapter

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_ownership(settings: GatewaySettings) -> tuple[Any, Any | None]:
    instance_id = f"gateway-{uuid4()}"
    if settings.ownership_backend != "redis":
        return LocalOwnershipManager(instance_id), None
    from redis.asyncio import Redis

    assert settings.redis_url is not None
    # A Redis that accepts TCP but never replies would otherwise wedge every
    # ownership call forever; the timeout keeps a failure inside the window where
    # the lease is still safe, so it is dropped locally and re-acquired once Redis
    # recovers.
    budget_ms = settings.lease_ttl_ms - settings.lease_safety_ms
    timeout = min(settings.lease_safety_ms, budget_ms / 2) / 1000
    redis = Redis.from_url(
        settings.redis_url,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
    )
    # URL options override keyword arguments; enforce the budget on the parsed pool.
    redis.connection_pool.connection_kwargs.update(
        socket_timeout=timeout, socket_connect_timeout=timeout
    )
    ownership = RedisOwnershipManager(
        redis,
        key_prefix=settings.redis_key_prefix,
        ttl_ms=settings.lease_ttl_ms,
        safety_ms=settings.lease_safety_ms,
        instance_id=instance_id,
    )
    return ownership, redis


async def _serve(settings: GatewaySettings) -> None:
    short_timeout = httpx.Timeout(10.0)
    stream_timeout = httpx.Timeout(
        connect=10.0,
        read=settings.workflow_timeout_seconds + 15.0,
        write=30.0,
        pool=10.0,
    )
    async with (
        httpx.AsyncClient(timeout=short_timeout) as control_client,
        httpx.AsyncClient(timeout=stream_timeout) as stream_client,
    ):
        ownership, redis = _build_ownership(settings)
        manager = ChannelManager(
            settings,
            ConfigClient(
                control_client,
                base_url=settings.api_url,
                token=settings.internal_token,
            ),
            WorkflowClient(
                stream_client,
                base_url=settings.api_url,
                token=settings.internal_token,
            ),
            StatusClient(
                control_client,
                base_url=settings.api_url,
                token=settings.internal_token,
            ),
            ownership,
            WecomAdapter,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(manager, settings),
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level.lower(),
                access_log=False,
            )
        )
        try:
            await server.serve()
        finally:
            if redis is not None:
                await redis.aclose()


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        settings = GatewaySettings.from_env(os.environ)
    except GatewayConfigurationError as error:
        logging.basicConfig(level=logging.ERROR)
        logger.error("invalid_gateway_configuration fields=%s", ",".join(error.fields))
        raise SystemExit(2) from None
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
