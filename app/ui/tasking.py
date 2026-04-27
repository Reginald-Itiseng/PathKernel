"""Qt thread/task helpers used to run blocking operations without freezing the UI."""

from __future__ import annotations

from datetime import datetime
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget


TaskFunc = Callable[[Callable[[str], None]], Any]


class TaskWorker(QObject):
    """Runs a callable in a background thread and streams log text."""

    finished = Signal(object)
    failed = Signal(str, str)
    log = Signal(str)

    def __init__(self, work: TaskFunc) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self.log.emit)
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())
            return
        self.finished.emit(result)


class TaskConsoleWidget(QWidget):
    """Small reusable terminal-like progress view for long-running tasks."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._title = QLabel("Task Console", self)
        self._title.setStyleSheet("color: #cde7cd; font-weight: 600;")
        layout.addWidget(self._title)

        self._output = QPlainTextEdit(self)
        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(300)
        mono = QFont("Consolas")
        mono.setPointSize(9)
        self._output.setFont(mono)
        self._output.setStyleSheet(
            """
            QPlainTextEdit {
                background-color: #0d1117;
                color: #7ee787;
                border: 1px solid #30363d;
                selection-background-color: #264f78;
            }
            """
        )
        layout.addWidget(self._output, 1)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(260)
        self._pulse_timer.timeout.connect(self._pulse_line)
        self._pulse_idx = 0
        self._pulse_frames = ["-", "\\", "|", "/"]
        self._pulse_msgs = [
            "checking geometry...",
            "building paths...",
            "resolving outlines...",
            "optimizing segments...",
        ]

    def start_task(self, title: str) -> None:
        self._title.setText(f"Task Console - {title}")
        self._output.clear()
        self.append_line(f"[{self._stamp()}] task start: {title}")
        self._pulse_idx = 0
        self._pulse_timer.start()

    def append_line(self, text: str) -> None:
        self._output.appendPlainText(text.rstrip())
        bar = self._output.verticalScrollBar()
        bar.setValue(bar.maximum())

    def finish_task(self, *, success: bool, detail: str = "") -> None:
        self._pulse_timer.stop()
        status = "done" if success else "failed"
        tail = f" ({detail})" if detail else ""
        self.append_line(f"[{self._stamp()}] task {status}{tail}")

    def _pulse_line(self) -> None:
        frame = self._pulse_frames[self._pulse_idx % len(self._pulse_frames)]
        msg = self._pulse_msgs[self._pulse_idx % len(self._pulse_msgs)]
        self._pulse_idx += 1
        self.append_line(f"[{self._stamp()}] {frame} {msg}")

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%H:%M:%S")


def start_worker_thread(parent: QObject, work: TaskFunc) -> tuple[QThread, TaskWorker]:
    """Create and wire a worker-thread pair without starting execution yet."""
    thread = QThread(parent)
    worker = TaskWorker(work)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    return thread, worker
