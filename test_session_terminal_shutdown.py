import errno
import unittest

from portacode.connection.handlers.session import TerminalSession


class _FakeStdout:
    async def read(self, _size: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = None
        self.returncode = 0
        self.pid = 1234


class TerminalSessionShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_io_forwarding_treats_pty_eio_as_eof(self) -> None:
        session = TerminalSession("term-1", _FakeProc(), channel=object())

        await session.start_io_forwarding()
        await session._reader_task

        self.assertTrue(session._reader_task.done())
        self.assertIsNone(session._reader_task.exception())
