"""Nonblocking POSIX storage lifecycle locks, acquired before any storage use."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class StorageMaintenanceBusy(RuntimeError):
    """Another process holds an incompatible storage lifecycle lock."""


class StorageMaintenanceLock:
    """Keep the fixed lock inode open until all workers/read handles are closed.

    Acquisition order is maintenance lock, workspace lock, database transaction.
    The lock file must never be removed or replaced, including during cleanup.
    """

    def __init__(self, storage_dir: Path, *, exclusive: bool) -> None:
        self._retain = False
        root = storage_dir.expanduser().resolve()
        if not exclusive:
            root.mkdir(parents=True, exist_ok=True)
        self.path = root / ".storage-maintenance.lock"
        self._fd: int | None = os.open(
            self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(
                self._fd,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self.close()
            raise StorageMaintenanceBusy(
                "storage is in use; stop API, workers and cleanup processes before maintenance"
            ) from exc
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._retain:
            return
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def retain_until_exit(self) -> None:
        """A failed worker shutdown cannot prove storage is idle; fail closed."""
        self._retain = True

    def __enter__(self) -> StorageMaintenanceLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
