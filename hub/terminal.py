"""A real terminal for the phone: a shell running in a pseudo-terminal.

Linux and macOS use the standard `pty` module. Windows uses pywinpty (ConPTY). Both give the
same four calls: read (blocks), write, resize, close.
"""
from __future__ import annotations

import os
import signal
import sys


class TerminalError(Exception):
    """The shell couldn't start. The message says why."""


def _env() -> dict[str, str]:
    return {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}


class PosixTerminal:
    def __init__(self, argv: list[str], rows: int, cols: int, cwd: str | None = None):
        import fcntl
        import pty
        import subprocess
        import termios

        master, slave = pty.openpty()
        self.fd = master
        self._termios, self._fcntl = termios, fcntl
        self.resize(rows, cols)

        def become_session_leader():  # makes the pty this shell's controlling terminal
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         preexec_fn=become_session_leader, close_fds=True,
                                         cwd=cwd or os.path.expanduser("~"), env=_env())
        except (FileNotFoundError, PermissionError) as exc:
            os.close(master)
            raise TerminalError(f"Can't start {argv[0]}: {exc.strerror or exc}") from None
        finally:
            os.close(slave)

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 65536)
        except OSError:  # the shell exited
            return b""

    def write(self, data: bytes) -> None:
        while data:
            written = os.write(self.fd, data)
            data = data[written:]

    def resize(self, rows: int, cols: int) -> None:
        import struct
        self._fcntl.ioctl(self.fd, self._termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self) -> None:
        """Hang up the shell and anything it started."""
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                self.proc.wait(timeout=1.5)
                break
            except Exception:  # still running: try the next, harder signal
                continue

    def release(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class WindowsTerminal:
    def __init__(self, argv: list[str], rows: int, cols: int, cwd: str | None = None):
        try:
            from winpty import PtyProcess
        except ImportError:
            raise TerminalError("The Windows terminal needs pywinpty. "
                                "Run: pip install pywinpty") from None
        try:
            self.proc = PtyProcess.spawn(argv, cwd=cwd or os.path.expanduser("~"),
                                         dimensions=(rows, cols))
        except Exception as exc:  # pywinpty raises assorted errors for a missing program
            raise TerminalError(f"Can't start {argv[0]}: {exc}") from None

    def read(self) -> bytes:
        try:
            return self.proc.read(65536).encode("utf-8")
        except EOFError:
            return b""

    def write(self, data: bytes) -> None:
        self.proc.write(data.decode("utf-8", "replace"))

    def resize(self, rows: int, cols: int) -> None:
        self.proc.setwinsize(rows, cols)

    def close(self) -> None:
        if self.proc.isalive():
            self.proc.terminate(force=True)

    def release(self) -> None:
        pass


def open_terminal(argv: list[str], rows: int = 24, cols: int = 80):
    cls = WindowsTerminal if sys.platform == "win32" else PosixTerminal
    return cls(argv, rows, cols)
