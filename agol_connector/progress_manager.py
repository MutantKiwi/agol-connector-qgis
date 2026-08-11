"""
progress_manager.py — Centralised loading progress for AGOL Connector
======================================================================

All load operations (browser panel, DSM, right-click, data items) call:

    pm = ProgressManager.instance()
    task_id = pm.start("India Index", "Feature Service")
    ...
    pm.finish(task_id)        # success
    pm.fail(task_id, "msg")   # failure

This shows a non-blocking progress item in the QGIS message bar:
    [⟳] Loading Feature Service 'India Index'...   [✕]

Multiple concurrent loads stack as separate items.
The message bar is always visible from any panel.
"""

from __future__ import annotations
import time
from typing import Optional


class ProgressManager:
    """Singleton — one per QGIS session."""

    _instance: Optional["ProgressManager"] = None

    @classmethod
    def instance(cls) -> "ProgressManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._iface   = None
        self._tasks:  dict[int, object] = {}   # task_id → QgsMessageBarItem
        self._next_id = 1

    def set_iface(self, iface):
        self._iface = iface

    # ── Public API ──────────────────────────────────────────────────────

    def start(self, name: str, kind: str = "") -> int:
        """
        Show a loading message in the QGIS message bar.
        Returns a task_id to pass to finish() or fail().
        """
        task_id = self._next_id
        self._next_id += 1

        label = f"Loading {kind} '{name}'…" if kind else f"Loading '{name}'…"
        self._show_bar(task_id, label)
        return task_id

    def finish(self, task_id: int, name: str = ""):
        """Remove the loading message and optionally show a brief success message."""
        self._remove_bar(task_id)
        if name and self._iface:
            try:
                from .compat import MSG_SUCCESS
                self._iface.messageBar().pushMessage(
                    "AGOL", f"'{name}' loaded.", MSG_SUCCESS, 3
                )
            except Exception:
                pass

    def fail(self, task_id: int, error: str):
        """Replace the loading message with a red error."""
        self._remove_bar(task_id)
        if self._iface:
            try:
                from .compat import MSG_CRITICAL
                self._iface.messageBar().pushMessage(
                    "AGOL", error, MSG_CRITICAL, 8
                )
            except Exception:
                pass

    def update(self, task_id: int, fetched: int, total: int):
        """Update a running task with a determinate progress value."""
        item = self._tasks.get(task_id)
        if item is None:
            return
        try:
            pbar = item._progress_bar
            if total > 0:
                pbar.setRange(0, total)
                pbar.setValue(fetched)
            else:
                pbar.setRange(0, 0)   # indeterminate
        except Exception:
            pass

    # ── Internal ─────────────────────────────────────────────────────────

    def _show_bar(self, task_id: int, label: str):
        if not self._iface:
            return
        try:
            from qgis.PyQt.QtWidgets import QProgressBar, QLabel, QHBoxLayout, QWidget
            from qgis.gui import QgsMessageBar

            bar: QgsMessageBar = self._iface.messageBar()

            # Build the widget: spinner label + text + progress bar
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)

            spinner = QLabel("⟳")
            spinner.setStyleSheet("font-size: 14px; color: #1a75d2;")
            row.addWidget(spinner)

            lbl = QLabel(label)
            lbl.setStyleSheet("font-weight: 500;")
            row.addWidget(lbl)

            pbar = QProgressBar()
            pbar.setRange(0, 0)          # indeterminate until update() is called
            pbar.setFixedWidth(120)
            pbar.setFixedHeight(14)
            pbar.setTextVisible(False)
            row.addWidget(pbar)

            from .compat import MSG_INFO
            item = bar.createMessage("AGOL", "")
            item.layout().addWidget(widget)

            # Store progress bar ref for update()
            item._progress_bar = pbar
            item._label        = lbl
            item._start_time   = time.time()

            bar.pushItem(item)
            self._tasks[task_id] = item

            # Animate spinner
            self._start_spinner(task_id, spinner)

        except Exception:
            pass   # message bar unavailable — silent degradation

    def _remove_bar(self, task_id: int):
        item = self._tasks.pop(task_id, None)
        if item is None or not self._iface:
            return
        try:
            self._iface.messageBar().popWidget(item)
        except Exception:
            pass

    def _start_spinner(self, task_id: int, spinner_label):
        """Rotate the spinner glyph using a QTimer."""
        try:
            from qgis.PyQt.QtCore import QTimer
            frames = ["⟳", "⟲", "↻", "↺"]
            state  = [0]

            def _tick():
                if task_id not in self._tasks:
                    return
                state[0] = (state[0] + 1) % len(frames)
                try:
                    spinner_label.setText(frames[state[0]])
                except RuntimeError:
                    pass   # widget deleted

            timer = QTimer()
            timer.setInterval(200)
            timer.timeout.connect(_tick)
            timer.start()
            # Keep timer alive by storing it on the item
            if task_id in self._tasks:
                self._tasks[task_id]._spinner_timer = timer
        except Exception:
            pass
