"""
dialogs/auth_dialog.py — SignInDialog
=====================================
Primary: Username / Password (covers almost all users).
Advanced (collapsible): OAuth2 for SSO/federated org accounts.
API Token removed — developer use only, adds no value for typical users.

Security notes:
  - Password sent as plaintext in HTTPS POST body (standard AGOL token auth).
    TLS protects it in transit — identical to logging into arcgis.com.
  - Credentials stored in QgsAuthManager (encrypted, QGIS master password).
  - Token expires in 60 min; plugin silently re-auths using stored creds.
  - OAuth2 option available for organisations that have disabled token auth.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QLabel,
    QDialogButtonBox, QCheckBox, QPushButton, QHBoxLayout,
    QFrame, QWidget, QMessageBox, QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, QUrl, QPropertyAnimation, QEasingCurve
from qgis.PyQt.QtGui import QDesktopServices, QFont

from ..agol_client import AGOLClient, AGOLAuthError


class SignInDialog(QDialog):

    def __init__(self, connection_name: str = "",
                 portal_url: str = "https://www.arcgis.com",
                 prefill_username: str = "",
                 parent=None):
        super().__init__(parent)
        self.client:   AGOLClient | None = None
        self.username: str = ""
        self.password: str = ""
        self.remember: bool = True

        self._conn_name  = connection_name
        self._url        = portal_url.rstrip("/")
        self._oauth_open = False

        title = f"Sign in — {connection_name}" if connection_name else "Sign in to ArcGIS Online"
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self._build_ui(prefill_username)
        self.adjustSize()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self, prefill_user: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Connection header
        if self._conn_name:
            name_lbl = QLabel(self._conn_name)
            f = QFont()
            f.setPointSize(12)
            f.setWeight(QFont.Weight.Medium)
            name_lbl.setFont(f)
            layout.addWidget(name_lbl)
            url_lbl = QLabel(self._url)
            url_lbl.setStyleSheet("color: palette(mid); font-size: 11px;")
            layout.addWidget(url_lbl)
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color: palette(mid);")
            layout.addWidget(line)

        # ── Primary: username / password ──────────────────────────────
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        self._user_edit = QLineEdit(prefill_user)
        self._user_edit.setPlaceholderText("ArcGIS username")
        form.addRow("Username", self._user_edit)

        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText("Password")
        self._pw_edit.returnPressed.connect(self._sign_in)
        form.addRow("Password", self._pw_edit)
        layout.addLayout(form)

        # Remember checkbox
        self._remember_cb = QCheckBox("Remember credentials")
        self._remember_cb.setChecked(True)
        layout.addWidget(self._remember_cb)

        # ── Advanced / OAuth2 toggle ───────────────────────────────────
        adv_row = QHBoxLayout()
        self._adv_btn = QPushButton("▶  Sign in with OAuth2 (SSO / federated accounts)")
        self._adv_btn.setFlat(True)
        self._adv_btn.setStyleSheet(
            "text-align:left; color: palette(link); font-size: 11px; border: none;"
        )
        self._adv_btn.clicked.connect(self._toggle_oauth)
        adv_row.addWidget(self._adv_btn)
        adv_row.addStretch()
        layout.addLayout(adv_row)

        # OAuth2 panel (hidden by default)
        self._oauth_panel = self._build_oauth_panel()
        self._oauth_panel.setVisible(False)
        layout.addWidget(self._oauth_panel)

        # Separator + sign-in note
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: palette(mid);")
        layout.addWidget(sep)

        note = QLabel(
            "Your password is sent securely over HTTPS and is never stored "
            "in plain text. For SSO/SAML accounts, use OAuth2 above."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(note)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in")
        btns.accepted.connect(self._sign_in)
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
        self._cid_edit = QLineEdit()
        self._cid_edit.setStyleSheet("background: palette(base);")
        self._cid_edit.setPlaceholderText("Client ID from your AGOL app registration")
        f.addRow("Client ID", self._cid_edit)
        layout.addLayout(f)

        open_btn = QPushButton("1.  Open authorisation URL in browser")
        open_btn.clicked.connect(self._open_oauth_url)
        layout.addWidget(open_btn)

        f2 = QFormLayout()
        self._code_edit = QLineEdit()
        self._code_edit.setStyleSheet("background: palette(base);")
        self._code_edit.setPlaceholderText("Paste the code from the browser redirect")
        f2.addRow("2.  Auth code", self._code_edit)
        layout.addLayout(f2)

        hint = QLabel(
            "Register at My Content → New Item → Application.\n"
            "Redirect URI:  urn:ietf:wg:oauth:2.0:oob"
        )
        hint.setStyleSheet("color: palette(mid); font-size: 11px; border: none;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    # ── OAuth2 toggle ───────────────────────────────────────────────────

    def _toggle_oauth(self):
        self._oauth_open = not self._oauth_open
        self._oauth_panel.setVisible(self._oauth_open)
        arrow = "▼" if self._oauth_open else "▶"
        self._adv_btn.setText(
            f"{arrow}  Sign in with OAuth2 (SSO / federated accounts)"
        )
        self.adjustSize()

    # ── Sign-in dispatch ────────────────────────────────────────────────

    def _sign_in(self):
        # If OAuth2 panel is open and has content, use OAuth2
        if self._oauth_open and self._cid_edit.text().strip():
            try:
                self._sign_in_oauth()
            except AGOLAuthError as e:
                QMessageBox.critical(self, "OAuth2 sign in failed", str(e))
            except Exception as e:
                QMessageBox.critical(self, "Sign in failed", str(e))
            return

        # Otherwise use username / password
        try:
            self._sign_in_userpass()
        except AGOLAuthError as e:
            msg = str(e)
            if ("federated" in msg.lower() or
                    "unable to generate" in msg.lower() or
                    "sso" in msg.lower()):
                # Show OAuth2 panel automatically
                if not self._oauth_open:
                    self._toggle_oauth()
                QMessageBox.warning(
                    self, "Username/password not available",
                    msg + "\n\nThe OAuth2 section has been opened for you."
                )
            else:
                QMessageBox.critical(self, "Sign in failed", msg)
        except Exception as e:
            QMessageBox.critical(self, "Sign in failed", str(e))

    def _sign_in_userpass(self):
        u = self._user_edit.text().strip()
        p = self._pw_edit.text()
        if not u or not p:
            raise AGOLAuthError("Username and password are required.")
        client = AGOLClient(portal_url=self._url)
        client.login_token(u, p, referer=self._url)
        self.client   = client
        self.username = u
        self.password = p
        self.remember = self._remember_cb.isChecked()
        self.accept()

    def _sign_in_oauth(self):
        cid  = self._cid_edit.text().strip()
        code = self._code_edit.text().strip()
        if not cid:
            raise AGOLAuthError("Client ID is required.")
        if not code:
            raise AGOLAuthError("Paste the authorisation code from the browser.")
        client = AGOLClient(portal_url=self._url)
        client.exchange_oauth_code(cid, code)
        self.client   = client
        self.username = client.username or ""
        self.password = ""
        self.remember = self._remember_cb.isChecked()
        self.accept()

    def _open_oauth_url(self):
        cid = self._cid_edit.text().strip()
        if not cid:
            QMessageBox.warning(self, "Missing Client ID", "Enter a Client ID first.")
            return
        tmp = AGOLClient(portal_url=self._url)
        QDesktopServices.openUrl(QUrl(tmp.login_oauth(cid)))
