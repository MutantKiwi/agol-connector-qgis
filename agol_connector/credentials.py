"""
credentials.py — Central credential management
===============================================
Single source of truth for all AGOL authentication.

Storage tiers:
  QgsSettings      connection name + URL (readable without master password)
  QgsAuthManager   username + password (encrypted, QGIS master password)
  In-memory cache  token per connection (session only, 60-min expiry)

Usage (all entry points):
    store = CredentialStore.instance()
    client = store.get_client("My Org")           # None if not connected
    client = store.ensure_client("My Org", parent) # prompts if needed
    store.auto_load()                              # silent re-auth on startup
"""

from __future__ import annotations
import json
from typing import Optional

from qgis.core import QgsSettings, QgsApplication
from .agol_client import AGOLClient, AGOLTokenExpiredError

_CONN_BASE = "AGOL/connections"   # QgsSettings group for name+URL
_AUTH_KEY  = "auth_id"            # sub-key under each connection group


def _auth_manager():
    return QgsApplication.authManager()


class CredentialStore:

    _inst: "CredentialStore | None" = None

    def __init__(self):
        self._clients: dict[str, AGOLClient] = {}   # name → live client

    @classmethod
    def instance(cls) -> "CredentialStore":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    # ── Connection registry (QgsSettings) ─────────────────────────────

    def connection_names(self) -> list[str]:
        s = QgsSettings()
        s.beginGroup(_CONN_BASE)
        names = s.childGroups()
        s.endGroup()
        return names

    def connection_url(self, name: str) -> str:
        return QgsSettings().value(
            f"{_CONN_BASE}/{name}/url", "https://www.arcgis.com"
        )

    def save_connection(self, name: str, url: str,
                        username: str = "", password: str = "",
                        old_name: str = "") -> None:
        """
        Persist a connection.  If username+password provided, store them
        in QgsAuthManager under a Basic auth config.
        If old_name differs from name the old entry is removed first.
        """
        s = QgsSettings()
        if old_name and old_name != name:
            self._remove_auth_config(old_name)
            s.remove(f"{_CONN_BASE}/{old_name}")
            self._clients.pop(old_name, None)

        s.setValue(f"{_CONN_BASE}/{name}/url", url.rstrip("/"))

        if username and password:
            auth_id = self._save_auth_config(name, username, password)
            s.setValue(f"{_CONN_BASE}/{name}/{_AUTH_KEY}", auth_id)

    def remove_connection(self, name: str) -> None:
        self._remove_auth_config(name)
        QgsSettings().remove(f"{_CONN_BASE}/{name}")
        self._clients.pop(name, None)

    def saved_username(self, name: str) -> str:
        """Return stored username for display purposes (no password)."""
        cfg = self._load_auth_config(name)
        return cfg.get("username", "") if cfg else ""

    # ── QgsAuthManager helpers ─────────────────────────────────────────

    def _save_auth_config(self, name: str, username: str,
                          password: str) -> str:
        """
        Store credentials in QgsAuthManager as a Basic auth config.
        Returns the config ID (a short hex string).
        """
        am = _auth_manager()
        if not am or not am.isDisabled():
            pass   # will work even without master password for Basic method

        try:
            from qgis.core import QgsAuthMethodConfig
            existing_id = QgsSettings().value(
                f"{_CONN_BASE}/{name}/{_AUTH_KEY}", ""
            )
            cfg = QgsAuthMethodConfig()
            if existing_id:
                am.loadAuthenticationConfig(existing_id, cfg, True)
            cfg.setName(f"AGOL - {name}")
            cfg.setMethod("Basic")
            cfg.setConfig("username", username)
            cfg.setConfig("password", password)
            if existing_id and cfg.id():
                am.updateAuthenticationConfig(cfg)
                return existing_id
            else:
                am.storeAuthenticationConfig(cfg)
                return cfg.id()
        except Exception:
            # QgsAuthManager not available — store base64 in QgsSettings
            import base64
            blob = base64.b64encode(
                json.dumps({"u": username, "p": password}).encode()
            ).decode()
            fake_id = f"agol_{name.replace(' ', '_')}"
            QgsSettings().setValue(f"{_CONN_BASE}/{name}/_cred", blob)
            return fake_id

    def _load_auth_config(self, name: str) -> Optional[dict]:
        """Return {"username": ..., "password": ...} or None."""
        am = _auth_manager()
        auth_id = QgsSettings().value(f"{_CONN_BASE}/{name}/{_AUTH_KEY}", "")

        # Try QgsAuthManager first
        if auth_id and not auth_id.startswith("agol_"):
            try:
                from qgis.core import QgsAuthMethodConfig
                cfg = QgsAuthMethodConfig()
                if am and am.loadAuthenticationConfig(auth_id, cfg, True):
                    u = cfg.config("username")
                    p = cfg.config("password")
                    if u:
                        return {"username": u, "password": p}
            except Exception:
                pass

        # Fallback: base64 in QgsSettings
        blob = QgsSettings().value(f"{_CONN_BASE}/{name}/_cred", "")
        if blob:
            try:
                import base64
                d = json.loads(base64.b64decode(blob.encode()).decode())
                return {"username": d.get("u", ""), "password": d.get("p", "")}
            except Exception:
                pass
        return None

    def _remove_auth_config(self, name: str) -> None:
        s = QgsSettings()
        auth_id = s.value(f"{_CONN_BASE}/{name}/{_AUTH_KEY}", "")
        if auth_id and not auth_id.startswith("agol_"):
            try:
                am = _auth_manager()
                if am:
                    am.removeAuthenticationConfig(auth_id)
            except Exception:
                pass
        s.remove(f"{_CONN_BASE}/{name}/_cred")

    # ── Client access ──────────────────────────────────────────────────

    def get_client(self, name: str) -> Optional[AGOLClient]:
        """Return live authenticated client, or None (no prompt)."""
        c = self._clients.get(name)
        return c if (c and c.token) else None

    def set_client(self, name: str, client: AGOLClient) -> None:
        self._clients[name] = client

    def is_signed_in(self, name: str) -> bool:
        return self.get_client(name) is not None

    # ── Auto-load on startup ───────────────────────────────────────────

    def auto_load(self) -> None:
        """
        Called once in initGui.  Silently authenticates every saved
        connection that has stored credentials.  Non-blocking — failures
        are silently ignored (user can connect manually).
        """
        for name in self.connection_names():
            if self.get_client(name):
                continue   # already connected
            creds = self._load_auth_config(name)
            if not creds:
                continue
            try:
                url = self.connection_url(name)
                client = AGOLClient(portal_url=url)
                client.login_token(
                    creds["username"], creds["password"], referer=url
                )
                self._clients[name] = client
            except Exception:
                pass   # will prompt on first use

    # ── ensure_client: prompts if needed ──────────────────────────────

    def ensure_client(self, name: str,
                      parent=None) -> Optional[AGOLClient]:
        """
        Return a live client for `name`.
        If not connected, open SignInDialog.
        parent: QWidget or None.
        """
        client = self.get_client(name)
        if client:
            return client

        from .dialogs.auth_dialog import SignInDialog
        url   = self.connection_url(name) if name else "https://www.arcgis.com"
        creds = self._load_auth_config(name) if name else None

        dlg = SignInDialog(
            connection_name = name,
            portal_url      = url,
            prefill_username= creds["username"] if creds else "",
            parent          = parent,
        )
        if dlg.exec():
            self._clients[name] = dlg.client
            # Persist credentials if user opted in
            if dlg.remember:
                self.save_connection(
                    name, url,
                    username = dlg.username,
                    password = dlg.password,
                )
            # Also populate the default slot so toolbar shows connected
            if not self._clients.get(""):
                self._clients[""] = dlg.client
            return dlg.client
        return None

    # ── Default (toolbar) client ───────────────────────────────────────

    def any_client(self) -> Optional[AGOLClient]:
        """Return any live client — used when no specific connection needed."""
        for c in self._clients.values():
            if c and c.token:
                return c
        return None

    def handle_token_expiry(self, name: str, parent=None) -> Optional[AGOLClient]:
        """
        Called when an AGOLTokenExpiredError is caught.
        Clears the expired token and attempts silent re-auth using saved
        credentials.  If that fails (no saved creds), prompts the user.
        Returns a fresh client, or None if re-auth fails/is cancelled.
        """
        self.sign_out(name)
        # Try silent re-auth first
        creds = self._load_auth_config(name) if name else None
        if creds and creds.get("username") and creds.get("password"):
            try:
                url    = self.connection_url(name) if name else "https://www.arcgis.com"
                client = AGOLClient(portal_url=url)
                client.login_token(
                    creds["username"], creds["password"], referer=url
                )
                self._clients[name] = client
                return client
            except Exception:
                pass
        # Prompt user
        return self.ensure_client(name, parent=parent)

    def sign_out_all(self) -> None:
        self._clients.clear()

    def sign_out(self, name: str) -> None:
        self._clients.pop(name, None)
