import time
from dataclasses import replace

import pytest

from app.channel_gateway.ownership import (
    AcquireInput,
    LocalOwnershipManager,
    RecoveryInput,
)
from app.channel_gateway.protocol import GatewayError


def acquire_input(
    bot_id: str = "bot-1", channel_id: str = "channel-1", revision: int = 1
) -> AcquireInput:
    return AcquireInput(bot_id=bot_id, channel_id=channel_id, revision=revision)


def test_generation_is_monotonic_across_bots():
    manager = LocalOwnershipManager("gateway-1")
    first = manager.acquire(acquire_input("bot-1"))
    second = manager.acquire(acquire_input("bot-2"))
    assert first is not None and second is not None
    assert second.generation == first.generation + 1
    assert first.token != second.token
    manager.release(first)
    third = manager.acquire(acquire_input("bot-1", revision=2))
    assert third is not None
    assert third.generation > second.generation


def test_second_owner_is_rejected_until_release():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    assert manager.acquire(acquire_input()) is None
    manager.release(lease)
    assert manager.acquire(acquire_input()) is not None


def test_higher_revision_clears_registered_conflict():
    manager = LocalOwnershipManager("gateway-1")
    first = manager.acquire(acquire_input(revision=1))
    assert first is not None
    assert manager.block(first) is True
    manager.release(first)

    with pytest.raises(GatewayError) as error:
        manager.acquire(acquire_input(revision=1))
    assert error.value.code == "CHANNEL_CONNECTION_CONFLICT"

    second = manager.acquire(acquire_input(revision=2))
    assert second is not None
    assert manager.acquire(acquire_input(revision=2)) is None


def test_stale_binding_and_revision_are_rejected():
    manager = LocalOwnershipManager("gateway-1")
    first = manager.acquire(acquire_input(revision=2))
    assert first is not None
    manager.release(first)

    with pytest.raises(GatewayError) as error:
        manager.acquire(acquire_input(channel_id="channel-2", revision=2))
    assert error.value.code == "CHANNEL_BINDING_STALE"

    with pytest.raises(GatewayError) as error:
        manager.acquire(acquire_input(revision=1))
    assert error.value.code == "CHANNEL_REVISION_STALE"

    assert manager.acquire(acquire_input(revision=3)) is not None


def test_expired_revision_only_allows_a_higher_revision():
    manager = LocalOwnershipManager("gateway-1")
    first = manager.acquire(acquire_input(revision=3))
    assert first is not None
    assert manager.publish_status(first, "offline", "CHANNEL_EXPIRED") is not None
    manager.release(first)

    with pytest.raises(GatewayError) as error:
        manager.acquire(acquire_input(revision=3))
    assert error.value.code == "CHANNEL_EXPIRED"

    with pytest.raises(GatewayError) as error:
        manager.recover(RecoveryInput("bot-1", "channel-1", 3, "request-1"))
    assert error.value.code == "CHANNEL_EXPIRED"

    assert manager.acquire(acquire_input(revision=4)) is not None


def test_renew_validates_the_whole_lease_identity():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    assert manager.renew(lease) is True
    assert manager.renew(replace(lease, token="other-token")) is False
    assert manager.renew(replace(lease, generation=lease.generation + 1)) is False
    assert manager.renew(replace(lease, channel_id="channel-2")) is False
    assert manager.renew(replace(lease, revision=2)) is False
    assert manager.renew(replace(lease, recovery_version=1)) is False
    assert manager.renew(lease) is True


def test_renew_fails_once_the_safe_window_has_elapsed():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    lease.safe_until = time.monotonic() - 1
    assert manager.is_valid(lease) is False
    assert manager.renew(lease) is False


def test_old_lease_cannot_renew_after_release_and_reacquire():
    manager = LocalOwnershipManager("gateway-1")
    first = manager.acquire(acquire_input())
    assert first is not None
    manager.release(first)
    second = manager.acquire(acquire_input())
    assert second is not None
    assert manager.renew(first) is False
    assert manager.is_valid(first) is False


def test_publish_and_read_status_require_a_live_owner():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input(revision=2))
    assert lease is not None

    state = manager.publish_status(lease, "online", None)
    assert state is not None
    assert state.channel_id == "channel-1"
    assert state.revision == 2
    assert state.status == "online"
    assert state.error_code is None
    assert manager.read_status(acquire_input(revision=2)) == state

    manager.release(lease)
    assert manager.read_status(acquire_input(revision=2)) is None


def test_online_status_never_carries_an_error_code():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    state = manager.publish_status(lease, "online", "AUTH_FAILED")
    assert state is not None
    assert state.error_code is None


def test_publish_status_rejects_a_released_lease():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    manager.release(lease)
    assert manager.publish_status(lease, "online", None) is None


def test_read_status_rejects_stale_channel_or_revision():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input(revision=2))
    assert lease is not None
    assert manager.publish_status(lease, "online", None) is not None
    assert (
        manager.read_status(acquire_input(channel_id="channel-2", revision=2)) is None
    )
    assert manager.read_status(acquire_input(revision=1)) is None


@pytest.mark.parametrize(
    ("status", "error_code", "block_first"),
    [
        ("offline", "CHANNEL_EXPIRED", False),
        ("error", "CONNECTION_CONFLICT", True),
        ("offline", "GATEWAY_STOPPED", False),
    ],
)
def test_terminal_status_is_readable_without_an_owner(
    status: str, error_code: str, block_first: bool
):
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    if block_first:
        assert manager.block(lease) is True
    state = manager.publish_status(lease, status, error_code)
    assert state is not None
    manager.release(lease)
    assert manager.read_status(acquire_input()) == state


def test_non_terminal_status_is_not_readable_without_an_owner():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    assert manager.publish_status(lease, "offline", None) is not None
    manager.release(lease)
    assert manager.read_status(acquire_input()) is None


def test_status_is_rejected_after_recovery_version_advances():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    assert manager.publish_status(lease, "online", None) is not None
    assert manager.block(lease) is True
    assert manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1")) is True
    assert manager.read_status(acquire_input()) is None


def test_block_requires_the_current_lease():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    assert manager.block(lease) is True
    manager.release(lease)
    assert manager.block(lease) is False


def test_recover_requires_a_conflict_or_a_dead_owner():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None

    with pytest.raises(GatewayError) as error:
        manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1"))
    assert error.value.code == "CHANNEL_RECOVERY_PENDING"
    assert error.value.retryable is True

    assert manager.block(lease) is True
    assert manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1")) is True
    assert manager.has_recovered(lease) is True
    assert manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1")) is False
    assert manager.renew(lease) is False
    assert lease.safe_until == 0


def test_recover_is_allowed_after_the_owner_is_released():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    manager.release(lease)
    assert manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-2")) is True
    assert manager.has_recovered(lease) is True


def test_recover_validates_binding_and_revision():
    manager = LocalOwnershipManager("gateway-1")
    with pytest.raises(GatewayError) as error:
        manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1"))
    assert error.value.code == "CHANNEL_BINDING_STALE"

    lease = manager.acquire(acquire_input(revision=2))
    assert lease is not None
    manager.release(lease)
    with pytest.raises(GatewayError) as error:
        manager.recover(RecoveryInput("bot-1", "channel-1", 1, "request-1"))
    assert error.value.code == "CHANNEL_REVISION_STALE"


def test_close_revokes_every_lease():
    manager = LocalOwnershipManager("gateway-1")
    lease = manager.acquire(acquire_input())
    assert lease is not None
    manager.close()
    assert manager.is_valid(lease) is False
    assert manager.acquire(acquire_input()) is None
