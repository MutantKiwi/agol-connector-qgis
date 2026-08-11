"""
logger.py — AGOL Connector logging
====================================
Writes structured log entries to:
  1. The QGIS "AGOL Connector" tab in the Log Messages panel (View → Panels)
  2. A rotating file log at <QGIS profile>/logs/agol_connector.log

Usage anywhere in the plugin:
    from .logger import log
    log.info("Loading feature service", url=url)
    log.warning("Token expired", connection=name)
    log.error("Upload failed", error=str(e), layer=layer_name)
    log.debug("addItem response", response=resp)

The file log always captures DEBUG+; the QGIS panel shows INFO+ by default.
"""

from __future__ import annotations
import os
import json
import datetime
from typing import Any


class AGOLLogger:

    TAG = "AGOL Connector"
    _instance: AGOLLogger | None = None

    @classmethod
    def instance(cls) -> "AGOLLogger":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._file_path: str | None = None
        self._file_ready: bool = False
        # Don't init file here — QGIS profile path not available yet at import time

    # ── File setup ───────────────────────────────────────────────────────

    def _init_file(self):
        try:
            from qgis.core import QgsApplication
            profile_dir = QgsApplication.qgisSettingsDirPath()
            log_dir = os.path.join(profile_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            self._file_path = os.path.join(log_dir, "agol_connector.log")
            # Rotate if over 5 MB
            if (os.path.exists(self._file_path) and
                    os.path.getsize(self._file_path) > 5 * 1024 * 1024):
                bak = self._file_path + ".1"
                if os.path.exists(bak):
                    os.unlink(bak)
                os.rename(self._file_path, bak)
        except Exception:
            self._file_path = None

    # ── Public API ───────────────────────────────────────────────────────

    def debug(self, message: str, **kwargs):
        self._log("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs):
        self._log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs):
        self._log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs):
        self._log("ERROR", message, **kwargs)

    def success(self, message: str, **kwargs):
        self._log("SUCCESS", message, **kwargs)

    # ── Internal ─────────────────────────────────────────────────────────

    def _log(self, level: str, message: str, **kwargs):
        # Lazy file init — defer until first write so QGIS profile path is available
        if not self._file_ready:
            self._init_file()
            self._file_ready = True

        ts  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra = ("  " + "  ".join(f"{k}={repr(v)}" for k, v in kwargs.items())
                 if kwargs else "")
        line = f"[{ts}] [{level:<7}] {message}{extra}"

        # QGIS Log Messages panel
        try:
            from qgis.core import QgsMessageLog, Qgis
            lvl_map = {
                "DEBUG":   Qgis.MessageLevel.Info,
                "INFO":    Qgis.MessageLevel.Info,
                "SUCCESS": Qgis.MessageLevel.Success,
                "WARNING": Qgis.MessageLevel.Warning,
                "ERROR":   Qgis.MessageLevel.Critical,
            }
            QgsMessageLog.logMessage(
                f"[{level}] {message}{extra}",
                self.TAG,
                lvl_map.get(level, Qgis.MessageLevel.Info),
            )
        except Exception:
            pass

        # File log
        if self._file_path:
            try:
                with open(self._file_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def open_log_file(self):
        """Open the log file in the system's default text editor."""
        if not self._file_path or not os.path.exists(self._file_path):
            return False
        try:
            from qgis.PyQt.QtGui import QDesktopServices
            from qgis.PyQt.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._file_path))
            return True
        except Exception:
            return False

    @property
    def log_file_path(self) -> str | None:
        return self._file_path


# Module-level shortcut
log = AGOLLogger.instance()
