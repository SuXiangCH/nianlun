"""Fixed-inode local media exclusion and renewable operation leases."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import logging
from sqlalchemy.exc import SQLAlchemyError
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from collections.abc import Generator

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


@contextmanager
def media_lock(
    root: Path, identities: list[tuple[str, str]]
) -> Generator[None, None, None]:
    """Never unlink lock files: every process must rendezvous on one inode."""
    root.mkdir(parents=True, exist_ok=True)
    held: list[tuple[int, threading.RLock]] = []
    try:
        for identity in sorted(set(identities)):
            key = hashlib.sha256(repr(identity).encode()).hexdigest()
            path = root / (key + ".lock")
            with _GUARD:
                lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())
            lock.acquire()
            try:
                fd = os.open(
                    path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
            except BaseException:
                lock.release()
                raise
            held.append((fd, lock))
        yield
    finally:
        for fd, lock in reversed(held):
            os.close(fd)
            lock.release()


class RenewingLease:
    def __init__(self, renew: Callable[[], bool]) -> None:
        self.renew = renew
        self.stop = threading.Event()
        self.valid = True
        self.thread = threading.Thread(
            target=self._run, name="image-lease", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.wait(30):
            try:
                self.valid = self.renew()
            except (OSError, RuntimeError, SQLAlchemyError):
                logging.getLogger(__name__).exception(
                    "answer_image.lease_renewal_failed"
                )
                self.valid = False
            if not self.valid:
                return

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
