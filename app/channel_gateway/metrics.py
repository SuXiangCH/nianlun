"""Thread-safe in-process counters exposed by the gateway control plane."""

from threading import Lock

METRIC_NAMES = (
    "connections",
    "messages_received",
    "messages_duplicate",
    "messages_unsupported",
    "replies_ok",
    "replies_failed",
    "replies_active",
    "reconnects",
    "lease_acquire_failed",
    "workflow_errors",
)


class Metrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[str, int] = {name: 0 for name in METRIC_NAMES}

    def inc(self, name: str, amount: int = 1) -> None:
        if name not in self._values:
            raise KeyError(name)
        with self._lock:
            self._values[name] += amount

    def dec(self, name: str, amount: int = 1) -> None:
        if name not in self._values:
            raise KeyError(name)
        with self._lock:
            self._values[name] = max(0, self._values[name] - amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)
