"""Best-effort control calls to WeCom channel gateway instances."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 12.0


class GatewayControlClient:
    def __init__(
        self,
        urls: tuple[str, ...],
        token: str | None,
        timeout_seconds: float,
    ) -> None:
        self.urls = tuple(url.rstrip("/") for url in urls)
        self.token = token
        self.timeout_seconds = timeout_seconds

    def refresh(self) -> None:
        for url in self.urls:
            self._post(url, "/api/channels/refresh", {}, self.timeout_seconds)

    def connect(self, channel_id: str, revision: int) -> dict[str, Any] | None:
        payload = {
            "channel_id": channel_id,
            "revision": revision,
            "request_id": uuid.uuid4().hex,
        }
        for url in self.urls:
            result = self._post(
                url, "/api/channels/connect", payload, _CONNECT_TIMEOUT_SECONDS
            )
            if result is not None:
                return result
        return None

    def _post(
        self, url: str, path: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any] | None:
        headers = (
            {"Authorization": f"Bearer {self.token}"} if self.token is not None else {}
        )
        try:
            response = httpx.post(
                url + path,
                json=payload,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "wecom.gateway_control_failed url=%s path=%s error=%s",
                url,
                path,
                type(exc).__name__,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "wecom.gateway_control_invalid_response url=%s path=%s", url, path
            )
            return None
        return data
