from dataclasses import replace

import pytest

from app.channel_gateway.config import GatewayConfigurationError, GatewaySettings

TOKENS = {
    "NIANLUN_INTERNAL_TOKEN": "i" * 32,
    "NIANLUN_GATEWAY_CONTROL_TOKEN": "c" * 32,
}


def load(**overrides: str) -> GatewaySettings:
    return GatewaySettings.from_env({**TOKENS, **overrides})


def test_defaults_match_the_gateway_design():
    settings = load()
    assert settings.host == "127.0.0.1"
    assert settings.port == 3100
    assert settings.log_level == "INFO"
    assert settings.api_url == "http://127.0.0.1:8000"
    assert settings.ownership_backend == "local"
    assert settings.redis_url is None
    assert settings.redis_key_prefix == "nianlun:channel-gateway"
    assert settings.lease_ttl_ms == 30000
    assert settings.lease_safety_ms == 5000
    assert settings.snapshot_interval_seconds == 30
    assert settings.status_heartbeat_seconds == 30
    assert settings.connect_timeout_seconds == 10
    assert settings.first_event_timeout_seconds == 30
    assert settings.workflow_timeout_seconds == 150
    assert settings.max_concurrent == 32
    assert settings.max_message_bytes == 65536
    assert settings.max_answer_bytes == 1048576
    assert settings.reply_max_bytes == 20480
    assert settings.stream_interval_ms == 300
    assert settings.stream_keepalive_seconds == 15
    assert settings.dedup_max_entries == 10000
    assert settings.dedup_ttl_seconds == 300
    assert settings.lease_renew_seconds == 10


@pytest.mark.parametrize("name", list(TOKENS))
def test_tokens_are_required(name: str):
    env = dict(TOKENS)
    env.pop(name)
    with pytest.raises(GatewayConfigurationError) as error:
        GatewaySettings.from_env(env)
    assert error.value.fields == [name]


@pytest.mark.parametrize("value", ["", "short", " " * 32])
def test_tokens_must_be_at_least_32_nonspace_characters(value: str):
    with pytest.raises(GatewayConfigurationError) as error:
        GatewaySettings.from_env({**TOKENS, "NIANLUN_GATEWAY_CONTROL_TOKEN": value})
    assert error.value.fields == ["NIANLUN_GATEWAY_CONTROL_TOKEN"]


@pytest.mark.parametrize("name", list(TOKENS))
def test_tokens_reject_surrounding_whitespace(name: str):
    with pytest.raises(GatewayConfigurationError) as error:
        GatewaySettings.from_env({**TOKENS, name: f" {TOKENS[name]} "})
    assert error.value.fields == [name]


def test_redis_backend_requires_a_url():
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_OWNERSHIP_BACKEND="redis")
    assert error.value.fields == ["REDIS_URL"]


@pytest.mark.parametrize("url", ["http://redis:6379", "redis://", "not a url"])
def test_redis_url_must_use_redis_scheme(url: str):
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_OWNERSHIP_BACKEND="redis", REDIS_URL=url)
    assert error.value.fields == ["REDIS_URL"]


def test_redis_backend_with_url_is_accepted():
    settings = load(
        GATEWAY_OWNERSHIP_BACKEND="redis", REDIS_URL="redis://127.0.0.1:6379/0"
    )
    assert settings.ownership_backend == "redis"
    assert settings.redis_url == "redis://127.0.0.1:6379/0"


@pytest.mark.parametrize("value", ["", "prefix:{tag}", "a{b", "a}b"])
def test_redis_prefix_rejects_braces(value: str):
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_REDIS_KEY_PREFIX=value)
    assert error.value.fields == ["GATEWAY_REDIS_KEY_PREFIX"]


def test_lease_ttl_must_exceed_safety_window():
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_LEASE_TTL_MS="5000", GATEWAY_LEASE_SAFETY_MS="5000")
    assert error.value.fields == ["GATEWAY_LEASE_TTL_MS"]


@pytest.mark.parametrize(
    ("ttl_ms", "safety_ms"),
    [(30000, 25000), (3000, 2500), (100, 99)],
)
def test_renewal_leaves_time_before_the_safe_deadline(ttl_ms: int, safety_ms: int):
    settings = load(
        GATEWAY_LEASE_TTL_MS=str(ttl_ms),
        GATEWAY_LEASE_SAFETY_MS=str(safety_ms),
    )
    assert 0 < settings.lease_renew_seconds <= (ttl_ms - safety_ms) / 2000


def test_first_event_timeout_must_be_shorter_than_workflow_timeout():
    with pytest.raises(GatewayConfigurationError) as error:
        load(
            GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS="150",
            GATEWAY_WORKFLOW_TIMEOUT_SECONDS="150",
        )
    assert error.value.fields == ["GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GATEWAY_PORT", "0"),
        ("GATEWAY_PORT", "70000"),
        ("GATEWAY_MAX_CONCURRENT", "0"),
        ("GATEWAY_MAX_MESSAGE_BYTES", "-1"),
        ("GATEWAY_SNAPSHOT_INTERVAL_SECONDS", "0"),
        ("GATEWAY_STREAM_INTERVAL_MS", "0"),
        ("GATEWAY_DEDUP_MAX_ENTRIES", "0"),
    ],
)
def test_non_positive_or_out_of_range_numbers_are_rejected(name: str, value: str):
    with pytest.raises(GatewayConfigurationError) as error:
        load(**{name: value})
    assert error.value.fields == [name]


def test_non_numeric_values_are_rejected():
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_PORT="abc")
    assert error.value.fields == ["GATEWAY_PORT"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "http://user:pass@example.com",
        "http://example.com?x=1",
        "http://example.com#fragment",
        "not a url",
    ],
)
def test_api_url_must_be_a_plain_http_service_url(url: str):
    with pytest.raises(GatewayConfigurationError) as error:
        load(NIANLUN_API_URL=url)
    assert error.value.fields == ["NIANLUN_API_URL"]


def test_ownership_backend_is_validated():
    with pytest.raises(GatewayConfigurationError) as error:
        load(GATEWAY_OWNERSHIP_BACKEND="etcd")
    assert error.value.fields == ["GATEWAY_OWNERSHIP_BACKEND"]


def test_configuration_error_never_contains_values():
    with pytest.raises(GatewayConfigurationError) as error:
        load(NIANLUN_INTERNAL_TOKEN="short-secret")
    assert "short-secret" not in str(error.value)
    assert "short-secret" not in repr(error.value.fields)


def test_settings_are_immutable():
    settings = load()
    with pytest.raises(AttributeError):
        settings.port = 1  # type: ignore[misc]
    assert replace(settings, port=3200).port == 3200
