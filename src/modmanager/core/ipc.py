"""Single-instance handoff: a second launch (e.g. from an nxm:// link) passes its
arguments to the running instance over a Unix socket and exits."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(runtime) / f"modmanager-{os.getuid()}.sock"


def send(message: str, path: Path | None = None) -> bool:
    """Deliver ``message`` to a running instance. False if none is listening."""
    path = path or socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(path))
            sock.sendall(message.encode("utf-8") + b"\n")
            return sock.recv(16) == b"ok\n"
    except OSError:
        return False


class Listener:
    """Accepts messages from later launches and hands each one to ``callback``."""

    def __init__(self, callback: Callable[[str], None], path: Path | None = None):
        self.callback = callback
        self.path = path or socket_path()
        self._sock: socket.socket | None = None

    def start(self) -> bool:
        if send("ping", self.path):
            return False  # Another instance owns the socket.
        try:
            self.path.unlink()  # Stale socket from a crashed instance.
        except FileNotFoundError:
            pass
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self._sock.listen(4)
        except OSError as exc:
            log.warning("Cannot listen for nxm links: %s", exc)
            return False
        threading.Thread(target=self._serve, name="ipc", daemon=True).start()
        return True

    def _serve(self) -> None:
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(3)
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 65536:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    conn.sendall(b"ok\n")
                except OSError:
                    continue
            message = data.decode("utf-8", "replace").strip()
            if message and message != "ping":
                self.callback(message)

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
