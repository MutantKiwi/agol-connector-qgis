"""
dialogs/connection_dialog.py — Add / Edit connection
Same auth structure as SignInDialog: username/password primary,
OAuth2 collapsible for SSO/federated accounts.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QDialogButtonBox, QMessageBox, QGroupBox,
    QPushButton, QHBoxLayout, QCheckBox, QLabel,
    QWidget, QFrame,
)
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices

from ..agol_client import AGOLClient, AGOLAuthError
from ..credentials import CredentialStore


class ConnectionDialog(QDialog):

    def __init__(self, name: str = "", url: str = "", parent=None):
        super().__init__(parent)
        self._orig             = name
        self.result_name:      str             = ""
        self.result_url:       str             = ""
        self.result_client:    AGOLClient | None = None
        self.entered_username: str             = ""
        self.entered_password: str             = ""
        self.save_credentials: bool            = False
        self._oauth_open = False

        store      = CredentialStore.instance()
        saved_user = store.saved_username(name) if name else ""

        self.setWindowTitle("New connection" if not name else f"Edit — {name}")
        self.setMinimumWidth(440)
        self._build_ui(name, url or "https://www.arcgis.com", saved_user)
        self.adjustSize()

    def _build_ui(self, name: str, url: str, saved_user: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Connection identity ────────────────────────────────────────
        id_box = QGroupBox("Connection")
        id_f   = QFormLayout(id_box)
        self._name = QLineEdit(name)
        self._name.setPlaceholderText("e.g.  My Organisation")
        id_f.addRow("Name *", self._name)
        self._url = QLineEdit(url)
        self._url.setPlaceholderText("https://www.arcgis.com")
        id_f.addRow("Portal URL *", self._url)
        layout.addWidget(id_box)

        # ── Credentials ────────────────────────────────────────────────
        cred_box = QGroupBox("Credentials (optional — sign in later if preferred)")
        cv = QVBoxLayout(cred_box)

        f = QFormLayout()
        self._user = QLineEdit(saved_user)
        self._user.setPlaceholderText("ArcGIS username")
        f.addRow("Username", self._user)
        self._pw = QLineEdit()
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("Leave blank to keep saved password")
        f.addRow("Password", self._pw)
        cv.addLayout(f)

        self._remember = QCheckBox("Remember credentials")
        self._remember.setChecked(bool(saved_user))
        cv.addWidget(self._remember)

        # OAuth2 toggle
        adv_row = QHBoxLayout()
        self._adv_btn = QPushButton("▶  Use OAuth2 instead (SSO / federated accounts)")
        self._adv_btn.setFlat(True)
        self._adv_btn.setStyleSheet(
            "text-align:left; color: palette(link); font-size: 11px; border: none;"
        )
        self._adv_btn.clicked.connect(self._toggle_oauth)
        adv_row.addWidget(self._adv_btn)
        adv_row.addStretch()
        cv.addLayout(adv_row)

        self._oauth_panel = self._build_oauth_panel()
        self._oauth_panel.setVisible(False)
        cv.addWidget(self._oauth_panel)

        hint = QLabel(
            "Credentials are encrypted by QGIS.\n"
            "Leave all fields blank to save without signing in."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        cv.addWidget(hint)
        layout.addWidget(cred_box)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _build_oauth_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(
            "QWidget { background: palette(alternateBase); "
            "border: 1px solid palette(mid); border-radius: 4px; }"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        f = QFormLayout()
        self._cid = QLineEdit()
        self._cid.setStyleSheet("background: palette(base);")
        self._cid.setPlaceholderText("Client ID from your AGOL app registration")
        f.addRow("Client ID", self._cid)
        layout.addLayout(f)

        open_btn = QPushButton("1.  Open authorisation URL in browser")
        open_btn.clicked.connect(self._open_oauth_url)
        layout.addWidget(open_btn)

        f2 = QFormLayout()
        self._code = QLineEdit()
        self._code.setStyleSheet("background: palette(base);")
        self._code.setPlaceholderText("Paste the code from the browser redirect")
        f2.addRow("2.  Auth code", self._code)
        layout.addLayout(f2)

        hint = QLabel(
            "Register at My Content → New Item → Application.\n"
            "Redirect URI:  urn:ietf:wg:oauth:2.0:oob"
        )
        hint.setStyleSheet("color: palette(mid); font-size: 11px; border: none;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _toggle_oauth(self):
        self._oauth_open = not self._oauth_open
        self._oauth_panel.setVisible(self._oauth_open)
        arrow = "▼" if self._oauth_open else "▶"
        self._adv_btn.setText(
            f"{arrow}  Use OAuth2 instead (SSO / federated accounts)"
        )
        # Clear username/password fields when switching to OAuth2
        if self._oauth_open:
            self._user.setEnabled(False)
            self._pw.setEnabled(False)
        else:
            self._user.setEnabled(True)
            self._pw.setEnabled(True)
        self.adjustSize()

    def _accept(self):
        name = self._name.text().strip()
        url  = self._url.text().strip().rstrip("/")

        if not name:
            QMessageBox.warning(self, "Missing name", "Please enter a connection name.")
            return
        if not url.startswith("http"):
            QMessageBox.warning(self, "Invalid URL", "URL must start with http(s)://")
            return

        self.result_name = name
        self.result_url  = url

        if self._oauth_open:
            # OAuth2 path
            cid  = self._cid.text().strip()
            code = self._code.text().strip()
            if cid and code:
                try:
                    client = AGOLClient(portal_url=url)
                    client.exchange_oauth_code(cid, code)
                    self.result_client    = client
                    self.save_credentials = self._remember.isChecked()
                except AGOLAuthError as e:
                    r = QMessageBox.question(
                        self, "OAuth2 failed",
                        f"{e}\n\nSave connection anyway without signing in?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if r != QMessageBox.StandardButton.Yes:
                        return
        else:
            # Username / password path
            u = self._user.text().strip()
            p = self._pw.text()
            if u and p:
                try:
                    client = AGOLClient(portal_url=url)
                    client.login_token(u, p, referer=url)
                    self.result_client    = client
                    self.entered_username = u
                    self.entered_password = p
                    self.save_credentials = self._remember.isChecked()
                except AGOLAuthError as e:
                    msg = str(e)
                    if ("federated" in msg.lower() or
                            "unable to generate" in msg.lower()):
                        if not self._oauth_open:
                            self._toggle_oauth()
                        QMessageBox.warning(
                            self, "Username/password not available",
                            msg + "\n\nThe OAuth2 section has been opened."
                        )
                        return
                    r = QMessageBox.question(
                        self, "Sign in failed",
                        f"{msg}\n\nSave connection anyway without signing in?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if r != QMessageBox.StandardButton.Yes:
                        return
                    self.entered_username = u
                    self.entered_password = p
                    self.save_credentials = self._remember.isChecked()
            elif u:
                self.entered_username = u
                self.save_credentials = self._remember.isChecked()

        self.accept()

    def _open_oauth_url(self):
        cid = self._cid.text().strip()
        url = self._url.text().strip() or "https://www.arcgis.com"
        if not cid:
            QMessageBox.warning(self, "Missing Client ID", "Enter a Client ID first.")
            return
        tmp = AGOLClient(portal_url=url)
        QDesktopServices.openUrl(QUrl(tmp.login_oauth(cid)))
