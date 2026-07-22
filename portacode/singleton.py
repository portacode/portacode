from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Optional


class GatewayLock:
    """A non-blocking, lifetime-held lock for the host gateway connection."""

    def __init__(self, path: Path):
        self.path = path
        self._file: Optional[IO[bytes]] = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True

        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o666)
        try:
            # Ensure a lock created under a restrictive umask remains usable by
            # another account on the same machine.
            if not sys.platform.startswith("win"):
                try:
                    os.fchmod(fd, 0o666)
                except PermissionError:
                    # A different user created it and already made it writable;
                    # opening it successfully is sufficient in that case.
                    pass
            lock_file = os.fdopen(fd, "r+b", buffering=0)
            fd = -1
            if not self._try_lock(lock_file):
                lock_file.close()
                return False
            self._file = lock_file
            return True
        finally:
            if fd >= 0:
                os.close(fd)

    @property
    def held(self) -> bool:
        return self._file is not None

    def write_pid(self, pid: int) -> None:
        if self._file is None:
            raise RuntimeError("gateway lock is not held")
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"{pid}\n".encode("ascii"))
        self._file.flush()

    def read_pid(self) -> Optional[int]:
        try:
            if self._file is not None:
                self._file.seek(0)
                raw = self._file.read()
            else:
                raw = self.path.read_bytes()
            return int(raw.strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._unlock(self._file)
        finally:
            self._file.close()
            self._file = None

    @staticmethod
    def _try_lock(lock_file: IO[bytes]) -> bool:
        if sys.platform.startswith("win"):
            import msvcrt

            try:
                lock_file.seek(0)
                # Lock one byte; ensure it exists first.
                if not lock_file.read(1):
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    @staticmethod
    def _unlock(lock_file: IO[bytes]) -> None:
        if sys.platform.startswith("win"):
            import msvcrt

            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return

        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "GatewayLock":
        if not self.acquire():
            raise RuntimeError("gateway lock is already held")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
