"""
dialogs/settings_dialog.py — Plugin settings
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QCheckBox,
    QSpinBox, QLineEdit, QLabel, QDialogButtonBox,
    QGroupBox, QComboBox,
)
from qgis.core import QgsSettings

_BASE = "AGOL/settings"


class SettingsDialog(QDialog):

    _KEY_AUTO_LOAD      = f"{_BASE}/auto_load"
    _KEY_MAX_FEATURES   = f"{_BASE}/max_features"
    _KEY_PAGE_SIZE      = f"{_BASE}/page_size"
    _KEY_DEFAULT_TAGS   = f"{_BASE}/default_tags"
    _KEY_SEARCH_LIMIT   = f"{_BASE}/search_limit"
    _KEY_TIMEOUT        = f"{_BASE}/timeout"
    _KEY_CACHE_RESULTS  = f"{_BASE}/cache_results"
    _KEY_TOKEN_EXPIRY   = f"{_BASE}/token_expiry_mins"
    _KEY_CRS            = f"{_BASE}/preferred_crs"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AGOL Connector — settings")
        self.setMinimumWidth(400)
        self._build_ui()
        self._load()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Authentication ─────────────────────────────────────────────
        auth_box = QGroupBox("Authentication")
        af = QFormLayout(auth_box)

        self._auto_load = QCheckBox("Sign in automatically on QGIS startup")
        af.addRow("", self._auto_load)

        self._token_expiry = QSpinBox()
        self._token_expiry.setRange(15, 20160)
        self._token_expiry.setSingleStep(15)
        self._token_expiry.setSuffix("  minutes")
        self._token_expiry.setToolTip(
            "How long tokens stay valid. AGOL default is 60 min (max 20160 = 2 weeks)."
        )
        af.addRow("Token expiry", self._token_expiry)
        layout.addWidget(auth_box)

        # ── Data ──────────────────────────────────────────────────────
        data_box = QGroupBox("Data download")
        df = QFormLayout(data_box)

        self._max_features = QSpinBox()
        self._max_features.setRange(100, 500000)
        self._max_features.setSingleStep(1000)
        self._max_features.setSuffix("  features")
        self._max_features.setToolTip(
            "Maximum total features fetched per layer. "
            "Large values may take a long time."
        )
        df.addRow("Max features per layer", self._max_features)

        self._page_size = QSpinBox()
        self._page_size.setRange(100, 5000)
        self._page_size.setSingleStep(100)
        self._page_size.setSuffix("  features")
        self._page_size.setToolTip(
            "Features requested per page. Overridden by the server's "
            "maxRecordCount if lower."
        )
        df.addRow("Page size (pagination)", self._page_size)

        self._search_limit = QSpinBox()
        self._search_limit.setRange(10, 1000)
        self._search_limit.setSingleStep(10)
        self._search_limit.setSuffix("  results")
        self._search_limit.setToolTip(
            "Maximum search results returned. AGOL paginates in pages of 100."
        )
        df.addRow("Search result limit", self._search_limit)

        self._timeout = QSpinBox()
        self._timeout.setRange(10, 300)
        self._timeout.setSingleStep(10)
        self._timeout.setSuffix("  seconds")
        self._timeout.setToolTip("HTTP request timeout for each API call.")
        df.addRow("Request timeout", self._timeout)

        self._preferred_crs = QLineEdit()
        self._preferred_crs.setPlaceholderText("e.g. EPSG:4326  (blank = service default)")
        self._preferred_crs.setToolTip(
            "Request features in this CRS. Leave blank to use the service's "
            "native CRS (recommended)."
        )
        df.addRow("Preferred output CRS", self._preferred_crs)
        layout.addWidget(data_box)

        # ── Upload ────────────────────────────────────────────────────
        upload_box = QGroupBox("Upload defaults")
        uf = QFormLayout(upload_box)

        self._default_tags = QLineEdit()
        self._default_tags.setPlaceholderText("qgis, upload")
        self._default_tags.setToolTip(
            "Default tags pre-filled in the upload dialog (comma-separated)."
        )
        uf.addRow("Default tags", self._default_tags)
        layout.addWidget(upload_box)

        # ── Buttons ───────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        btns.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self._restore_defaults)
        layout.addWidget(btns)

    def _load(self):
        s = QgsSettings()
        self._auto_load.setChecked(
            s.value(self._KEY_AUTO_LOAD, True, type=bool)
        )
        self._max_features.setValue(
            int(s.value(self._KEY_MAX_FEATURES, 10000))
        )
        self._page_size.setValue(
            int(s.value(self._KEY_PAGE_SIZE, 2000))
        )
        self._search_limit.setValue(
            int(s.value(self._KEY_SEARCH_LIMIT, 100))
        )
        self._timeout.setValue(
            int(s.value(self._KEY_TIMEOUT, 30))
        )
        self._token_expiry.setValue(
            int(s.value(self._KEY_TOKEN_EXPIRY, 60))
        )
        self._default_tags.setText(
            s.value(self._KEY_DEFAULT_TAGS, "qgis,upload")
        )
        self._preferred_crs.setText(
            s.value(self._KEY_CRS, "")
        )

    def _save(self):
        s = QgsSettings()
        s.setValue(self._KEY_AUTO_LOAD,    self._auto_load.isChecked())
        s.setValue(self._KEY_MAX_FEATURES, self._max_features.value())
        s.setValue(self._KEY_PAGE_SIZE,    self._page_size.value())
        s.setValue(self._KEY_SEARCH_LIMIT, self._search_limit.value())
        s.setValue(self._KEY_TIMEOUT,      self._timeout.value())
        s.setValue(self._KEY_TOKEN_EXPIRY, self._token_expiry.value())
        s.setValue(self._KEY_DEFAULT_TAGS, self._default_tags.text().strip())
        s.setValue(self._KEY_CRS,          self._preferred_crs.text().strip())
        self.accept()

    def _restore_defaults(self):
        self._auto_load.setChecked(True)
        self._max_features.setValue(10000)
        self._page_size.setValue(2000)
        self._search_limit.setValue(100)
        self._timeout.setValue(30)
        self._token_expiry.setValue(60)
        self._default_tags.setText("qgis,upload")
        self._preferred_crs.setText("")

    # ── Static accessors used by other modules ─────────────────────────

    @staticmethod
    def max_features() -> int:
        return int(QgsSettings().value(
            SettingsDialog._KEY_MAX_FEATURES, 10000
        ))

    @staticmethod
    def page_size() -> int:
        return int(QgsSettings().value(
            SettingsDialog._KEY_PAGE_SIZE, 2000
        ))

    @staticmethod
    def search_limit() -> int:
        return int(QgsSettings().value(
            SettingsDialog._KEY_SEARCH_LIMIT, 100
        ))

    @staticmethod
    def timeout() -> int:
        return int(QgsSettings().value(
            SettingsDialog._KEY_TIMEOUT, 30
        ))

    @staticmethod
    def token_expiry_mins() -> int:
        return int(QgsSettings().value(
            SettingsDialog._KEY_TOKEN_EXPIRY, 60
        ))

    @staticmethod
    def default_tags() -> str:
        return QgsSettings().value(
            SettingsDialog._KEY_DEFAULT_TAGS, "qgis,upload"
        )

    @staticmethod
    def preferred_crs() -> str:
        return QgsSettings().value(SettingsDialog._KEY_CRS, "")
