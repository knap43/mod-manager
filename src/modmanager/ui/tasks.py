"""Run slow operations (deploying, extracting) off the UI thread."""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)


class Task(QRunnable):
    """Calls ``fn(progress)`` in the thread pool; ``progress(done, total)`` is thread-safe."""

    def __init__(self, fn: Callable[[Callable[[int, int], None]], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            log.debug("Task failed:\n%s", traceback.format_exc())
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.done.emit(result)


_running: set[Task] = set()


def start(
    fn: Callable[[Callable[[int, int], None]], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Task:
    task = Task(fn)
    _running.add(task)

    def finish(_=None):
        _running.discard(task)

    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    if on_progress:
        task.signals.progress.connect(on_progress)
    task.signals.done.connect(finish)
    task.signals.failed.connect(finish)
    QThreadPool.globalInstance().start(task)
    return task
