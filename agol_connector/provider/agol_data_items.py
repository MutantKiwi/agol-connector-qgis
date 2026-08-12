"""
provider/agol_data_items.py  —  QGIS 3 & 4 compatible
=======================================================
Browser panel hierarchy:
  AGOL (root)
    └─ Connection name
         ├─ Home  (folder)
         │    └─ Service  →  Layer (double-click or drag to add)
         └─ Other folder…

Context menus are handled by AGOLDataItemGuiProviderBrowser (registered
in agol_connector.py) so they work in both QGIS 3 and QGIS 4.

All authentication goes through CredentialStore.
"""

from __future__ import annotations
import os
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox

from qgis.core import (
    QgsApplication,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsLayerItem,
    QgsMimeDataUtils,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
)

from ..agol_client import AGOLClient, SERVICE_TYPES
from ..compat import (
    STATE_POPULATED, TYPE_MESSAGE,
    LT_VECTOR, LT_RASTER, provider_capabilities,
)
from ..credentials import CredentialStore


# ── Helpers ────────────────────────────────────────────────────────────────

def _icon(name: str) -> QIcon:
    icon = QgsApplication.getThemeIcon(name)
    return icon if not icon.isNull() else QIcon()


def _plugin_icon(filename: str) -> QIcon:
    path = os.path.join(os.path.dirname(__file__), "..", "resources", filename)
    return QIcon(path) if os.path.exists(path) else QIcon()


def _fmt_epoch(ms: int) -> str:
    if not ms:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _err_item(parent: QgsDataItem, msg: str) -> QgsDataItem:
    return QgsDataItem(TYPE_MESSAGE, parent, msg, parent.path() + "/err")


# ══════════════════════════════════════════════════════════════════════════
#  Layer item  (leaf — one feature/raster layer)
# ══════════════════════════════════════════════════════════════════════════

class AGOLLayerItem(QgsDataItem):
    """
    Leaf node for a single layer.
    Intentionally does NOT extend QgsLayerItem so that QGIS does not
    auto-inject "Add Layer to Project" / "Open with Data Source Manager"
    into the context menu — we control the menu entirely ourselves.
    """

    def __init__(self, parent, name, layer_url, service_kind, client):
        # Use Custom type so QGIS treats it as a generic data item
        super().__init__(TYPE_MESSAGE, parent, name,
                         f"{parent.path()}/{name}")
        self._client       = client
        self._layer_url    = layer_url
        self._service_kind = service_kind
        self.setState(STATE_POPULATED)
        # Icon set later via set_geometry_icon(); default to feature/raster
        if service_kind == "feature":
            self.setIcon(_plugin_icon("icon_polygon.png"))
        else:
            self.setIcon(QgsApplication.getThemeIcon("/mIconRaster.svg"))

    def mimeUri(self):
        u = QgsMimeDataUtils.Uri()
        u.layerType   = "vector" if self._service_kind == "feature" else "raster"
        u.providerKey = "ogr"
        u.name        = self.name()
        u.uri         = self._layer_url
        return u

    def hasDragEnabled(self) -> bool:
        return True

    def handleDoubleClick(self) -> bool:
        self._load()
        return True

    def set_geometry_icon(self, geom_type: str):
        """Update icon once geometry type is known (from service detail)."""
        geom = geom_type.replace("esriGeometry", "").lower()
        if "point" in geom or "multipoint" in geom:
            self.setIcon(_plugin_icon("icon_point.png"))
        elif "line" in geom or "polyline" in geom:
            self.setIcon(_plugin_icon("icon_line.png"))
        else:
            self.setIcon(_plugin_icon("icon_polygon.png"))

    def _load(self):
        from ..dialogs.browser_dialog import _load_layer_from_item
        _load_layer_from_item(
            self._client, self._layer_url, self._service_kind, self.name()
        )

    # Store meta for properties dialog
    @property
    def layer_url(self):
        return self._layer_url

    @property
    def service_kind(self):
        return self._service_kind

    @property
    def client(self):
        return self._client


# ══════════════════════════════════════════════════════════════════════════
#  Service item
# ══════════════════════════════════════════════════════════════════════════

class AGOLServiceItem(QgsDataCollectionItem):

    _KIND_ICONS = {
        "feature": "icon_feature_service.png",
        "map":     "icon_map_service.png",
        "image":   "icon_image_service.png",
        "tile":    "icon_tile_service.png",
        "vtile":   "icon_vtile_service.png",
        "webmap":  "icon_webmap.png",
    }

    def __init__(self, parent, title, item_meta, client):
        super().__init__(parent, title, f"{parent.path()}/{title}")
        self._meta   = item_meta
        self._client = client
        self._kind   = SERVICE_TYPES.get(item_meta.get("type", ""), "feature")
        owner    = item_meta.get("owner", "")
        modified = _fmt_epoch(item_meta.get("modified", 0))
        tip = title
        if owner:
            tip += f"\nOwner: {owner}"
        if modified:
            tip += f"\nModified: {modified}"
        self.setToolTip(tip)

        # Set icon: try AGOL item thumbnail first, fall back to type icon
        self._set_service_icon(item_meta)

    def _set_service_icon(self, item_meta: dict) -> None:
        """
        Use the AGOL item thumbnail if available (matches what ArcGIS REST
        Server browser shows), otherwise fall back to a QGIS theme icon
        based on service type.
        """
        # Set icon from our custom PNGs
        icon_file = self._KIND_ICONS.get(self._kind, "icon_feature_service.png")
        self.setIcon(_plugin_icon(icon_file))

        # Try to load thumbnail async from AGOL
        thumbnail = item_meta.get("thumbnail", "")
        item_id   = item_meta.get("id", "")
        portal    = getattr(self._client, "portal_url",
                            "https://www.arcgis.com").rstrip("/")
        if thumbnail and item_id:
            thumb_url = (f"{portal}/sharing/rest/content/items/"
                         f"{item_id}/info/{thumbnail}")
            self._load_thumbnail_async(thumb_url)

    def _load_thumbnail_async(self, url: str) -> None:
        """Fetch thumbnail bytes in background and set as icon."""
        import urllib.request
        from qgis.PyQt.QtCore import QThread, pyqtSignal

        class _TW(QThread):
            done = pyqtSignal(bytes)
            def __init__(self, u):
                super().__init__()
                self._u = u
            def run(self):
                try:
                    req = urllib.request.Request(
                        self._u,
                        headers={"User-Agent": "QGIS-AGOL-Connector/0.1"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as r:
                        self.done.emit(r.read())
                except Exception:
                    pass

        def _apply(data: bytes):
            try:
                from qgis.PyQt.QtGui import QPixmap, QIcon
                from qgis.PyQt.QtCore import QByteArray
                pix = QPixmap()
                if pix.loadFromData(QByteArray(data)):
                    self.setIcon(QIcon(pix.scaled(
                        16, 16,
                        __import__("qgis.PyQt.QtCore", fromlist=["Qt"]).Qt.AspectRatioMode.KeepAspectRatio,
                        __import__("qgis.PyQt.QtCore", fromlist=["Qt"]).Qt.TransformationMode.SmoothTransformation,
                    )))
            except Exception:
                pass

        tw = _TW(url)
        tw.done.connect(_apply)
        tw.finished.connect(tw.deleteLater)
        tw.start()
        self._thumb_worker = tw   # keep reference

    def createChildren(self):
        # Web Maps: list their operational layers
        if self._kind == "webmap":
            item_id = self._meta.get("id", "")
            if not item_id:
                return []
            try:
                layers = self._client.get_web_map_layers(item_id)
            except Exception as e:
                return [_err_item(self, f"Error: {e}")]
            return [
                AGOLLayerItem(
                    self,
                    lyr.get("title", "Layer"),
                    lyr.get("url", ""),
                    "feature" if "Feature" in lyr.get("layerType", "") else "map",
                    self._client,
                )
                for lyr in layers if lyr.get("url")
            ]

        svc_url = self._meta.get("url", "")
        if not svc_url:
            return []
        try:
            layers = self._client.get_service_layers(svc_url)
        except Exception as e:
            return [_err_item(self, f"Error: {e}")]
        items = []
        for lyr in layers:
            li = AGOLLayerItem(
                self,
                lyr.get("name", f"Layer {lyr['id']}"),
                f"{svc_url.rstrip('/')}/{lyr['id']}",
                self._kind,
                self._client,
            )
            geom = lyr.get("geometryType", "")
            if geom and self._kind == "feature":
                li.set_geometry_icon(geom)
            items.append(li)
        return items

    def _add_first_layer(self):
        """Add the first (or only) layer to the map. Used by browser right-click."""
        if self._kind == "webmap":
            self._add_all_layers()
            return
        svc_url = self._meta.get("url", "")
        if not svc_url:
            return
        from ..dialogs.browser_dialog import _load_layer_from_item
        try:
            layers = self._client.get_service_layers(svc_url)
            url = f"{svc_url.rstrip('/')}/{layers[0]['id']}" if layers else svc_url
        except Exception:
            url = svc_url
        _load_layer_from_item(self._client, url, self._kind,
                              self._meta.get("title", self.name()))

    def _add_all_layers(self):
        """Add all layers of this service to the map."""
        if self._kind == "webmap":
            item_id = self._meta.get("id", "")
            try:
                layers = self._client.get_web_map_layers(item_id)
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox
                QMessageBox.critical(None, "AGOL", str(e))
                return
            from ..dialogs.browser_dialog import _load_layer_from_item
            for lyr in layers:
                if lyr.get("url"):
                    kind = "feature" if "Feature" in lyr.get("layerType","") else "map"
                    _load_layer_from_item(
                        self._client, lyr["url"], kind, lyr.get("title","Layer")
                    )
            return
        svc_url = self._meta.get("url", "")
        if not svc_url:
            return
        from ..dialogs.browser_dialog import _load_layer_from_item
        try:
            layers = self._client.get_service_layers(svc_url)
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(None, "AGOL", str(e))
            return
        for lyr in layers:
            url = f"{svc_url.rstrip('/')}/{lyr['id']}"
            _load_layer_from_item(self._client, url, self._kind,
                                  lyr.get("name", "Layer"))


# ══════════════════════════════════════════════════════════════════════════
#  Folder item
# ══════════════════════════════════════════════════════════════════════════

class AGOLFolderItem(QgsDataCollectionItem):

    def __init__(self, parent, title, folder_id, client):
        super().__init__(parent, title, f"{parent.path()}/{title}")
        self._folder_id = folder_id
        self._client    = client
        self.setIcon(_plugin_icon("icon_folder.png"))

    def createChildren(self):
        try:
            items = self._client.get_folder_items(self._folder_id)
        except Exception as e:
            return [_err_item(self, f"Error: {e}")]
        return [
            AGOLServiceItem(self, i.get("title", "—"), i, self._client)
            for i in items
        ]


# ══════════════════════════════════════════════════════════════════════════
#  Connection item
# ══════════════════════════════════════════════════════════════════════════

class AGOLConnectionItem(QgsDataCollectionItem):

    def __init__(self, parent, name, url):
        super().__init__(parent, name, f"AGOL/{name}")
        self._conn_name = name
        self._url       = url
        self.setIcon(QgsApplication.getThemeIcon("/mIconConnect.svg"))

    def _client(self) -> Optional[AGOLClient]:
        return CredentialStore.instance().get_client(self._conn_name)

    def _is_connected(self) -> bool:
        return self._client() is not None

    def createChildren(self):
        # Try the store first (may be auto-loaded)
        client = self._client()
        if not client:
            # Non-interactive fallback: store can silently re-auth if
            # credentials were saved.  This runs in a QGIS worker thread
            # so we must NOT show a dialog — just return the hint item.
            return [_err_item(
                self,
                "Not connected — right-click → Connect…",
            )]
        try:
            folders = client.get_user_folders()
        except Exception as e:
            return [_err_item(self, f"Error: {e}")]

        children = [AGOLFolderItem(self, "Home", "", client)]
        for f in folders:
            children.append(
                AGOLFolderItem(self, f.get("title", "—"),
                               f.get("id", ""), client)
            )
        return children


# ══════════════════════════════════════════════════════════════════════════
#  Root item
# ══════════════════════════════════════════════════════════════════════════

class AGOLRootItem(QgsDataCollectionItem):

    def __init__(self):
        super().__init__(None, "AGOL", "AGOL")
        self.setIcon(_plugin_icon("icon_agol_root.png"))
        self.setSortKey("AGOL")

    def createChildren(self):
        store = CredentialStore.instance()
        names = store.connection_names()
        if not names:
            tip = _err_item(self, "No connections — right-click to add one")
            return [tip]
        return [
            AGOLConnectionItem(self, n, store.connection_url(n))
            for n in names
        ]


# ══════════════════════════════════════════════════════════════════════════
#  Data item provider
# ══════════════════════════════════════════════════════════════════════════

class AGOLDataItemProvider(QgsDataItemProvider):

    def name(self) -> str:
        return "AGOL"

    def capabilities(self):
        # QGIS 4.x (PyQt6 SIP): must return the enum member directly —
        # no int, no OR operation. SIP maps the enum to Capabilities automatically.
        # Try each form in order: nested enum (4.x), flat enum (3.x), raw int.
        from qgis.core import QgsDataItemProvider as _P
        for attr in (
            lambda: _P.Capability.Other,     # QGIS 4.x nested
            lambda: _P.Other,                # QGIS 3.x flat
        ):
            try:
                return attr()
            except AttributeError:
                continue
        # Absolute last resort — return whatever super() gives (may be 0/NoCapabilities)
        return super().capabilities()

    def createDataItem(self, path: str, parent) -> Optional[QgsDataItem]:
        if path in ("", "/"):
            return AGOLRootItem()
        return None

    def handlesDirectoryPath(self, path: str) -> bool:
        return False


# ══════════════════════════════════════════════════════════════════════════
#  GUI provider for browser context menus
#  Registered via QgsGui.dataItemGuiProviderRegistry()
# ══════════════════════════════════════════════════════════════════════════

class AGOLBrowserGuiProvider:
    """
    Handles right-click menus for all AGOL browser nodes.
    This is separate from the file-export provider in browser_provider.py.
    """

    def __init__(self, iface):
        self._iface = iface
        self._store = CredentialStore.instance()

    def populate_menu(self, item: QgsDataItem, menu: QMenu) -> None:
        """Call this from the QgsDataItemGuiProvider.populateContextMenu."""
        from qgis.core import QgsApplication
        refresh_icon = QgsApplication.getThemeIcon("/mActionRefresh.svg")

        if isinstance(item, AGOLRootItem):
            a = QAction("New connection…", menu)
            a.triggered.connect(lambda: self._new_connection(item))
            menu.addAction(a)
            menu.addSeparator()
            ra = QAction(refresh_icon, "Refresh", menu)
            ra.triggered.connect(lambda: item.refresh())
            menu.addAction(ra)

        elif isinstance(item, AGOLConnectionItem):
            menu.addSeparator()
            if item._is_connected():
                c = self._store.get_client(item._conn_name)
                label = f"Disconnect ({c.username})" if c else "Disconnect"
                dc = QAction(label, menu)
                dc.triggered.connect(lambda: self._disconnect(item))
                menu.addAction(dc)
            else:
                cn = QAction("Connect…", menu)
                cn.triggered.connect(lambda: self._connect(item))
                menu.addAction(cn)
            menu.addSeparator()
            # Refresh — re-fetch folder list, keep existing token
            ref = QAction(refresh_icon, "Refresh", menu)
            ref.triggered.connect(lambda: item.refresh())
            menu.addAction(ref)
            # Refresh + re-authenticate — clears token first
            reauth = QAction(refresh_icon, "Refresh and re-authenticate…", menu)
            reauth.triggered.connect(lambda: self._refresh_reauth(item))
            menu.addAction(reauth)
            menu.addSeparator()
            ed = QAction("Edit connection…", menu)
            ed.triggered.connect(lambda: self._edit_connection(item))
            menu.addAction(ed)
            rm = QAction("Remove connection", menu)
            rm.triggered.connect(lambda: self._remove_connection(item))
            menu.addAction(rm)

        elif isinstance(item, AGOLFolderItem):
            add_all = QAction("Add all to map", menu)
            add_all.triggered.connect(lambda: self._add_folder_to_map(item))
            menu.addAction(add_all)
            menu.addSeparator()
            ref = QAction(refresh_icon, "Refresh", menu)
            ref.triggered.connect(lambda: item.refresh())
            menu.addAction(ref)

        elif isinstance(item, AGOLServiceItem):
            add = QAction("Add to map", menu)
            add.triggered.connect(lambda: item._add_first_layer())
            menu.addAction(add)
            add_all = QAction("Add all layers to map", menu)
            add_all.triggered.connect(lambda: item._add_all_layers())
            menu.addAction(add_all)
            menu.addSeparator()
            ref = QAction(refresh_icon, "Refresh", menu)
            ref.triggered.connect(lambda: item.refresh())
            menu.addAction(ref)
            menu.addSeparator()
            del_act = QAction("Delete…", menu)
            del_act.triggered.connect(lambda: self._delete_item(item))
            menu.addAction(del_act)

        elif isinstance(item, AGOLLayerItem):
            add = QAction("Add to map", menu)
            add.triggered.connect(lambda: item._load())
            menu.addAction(add)
            menu.addSeparator()
            del_act = QAction("Delete…", menu)
            del_act.triggered.connect(lambda: self._delete_layer(item))
            menu.addAction(del_act)

    # ── connection actions ─────────────────────────────────────────────

    def _connect(self, item: AGOLConnectionItem):
        parent = self._iface.mainWindow() if self._iface else None
        client = self._store.ensure_client(item._conn_name, parent=parent)
        if client:
            item.refresh()
            if item.parent():
                item.parent().refresh()

    def _refresh_reauth(self, item: AGOLConnectionItem):
        """Clear the existing token and force a fresh sign-in."""
        self._store.sign_out(item._conn_name)
        parent = self._iface.mainWindow() if self._iface else None
        client = self._store.ensure_client(item._conn_name, parent=parent)
        if client:
            item.refresh()
        else:
            item.refresh()

    def _add_folder_to_map(self, item: AGOLFolderItem):
        """Add every service in the folder to the map."""
        try:
            services = item._client.get_folder_items(item._folder_id)
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(None, "AGOL", str(e))
            return
        from ..dialogs.browser_dialog import _load_layer_from_item
        from ..agol_client import SERVICE_TYPES
        for svc in services:
            kind = SERVICE_TYPES.get(svc.get("type", ""), "feature")
            url  = svc.get("url", "")
            if not url:
                continue
            try:
                if kind == "webmap":
                    layers = item._client.get_web_map_layers(svc.get("id",""))
                    for lyr in layers:
                        if lyr.get("url"):
                            k = "feature" if "Feature" in lyr.get("layerType","") else "map"
                            _load_layer_from_item(item._client, lyr["url"], k,
                                                  lyr.get("title","Layer"))
                else:
                    layers = item._client.get_service_layers(url)
                    for lyr in layers:
                        lurl = f"{url.rstrip('/')}/{lyr['id']}"
                        _load_layer_from_item(item._client, lurl, kind,
                                              lyr.get("name","Layer"))
            except Exception:
                pass

    def _delete_item(self, item: AGOLServiceItem):
        """Delete a service item from AGOL after confirmation."""
        from qgis.PyQt.QtWidgets import QMessageBox
        parent = self._iface.mainWindow() if self._iface else None
        name   = item.name()
        msg = "Permanently delete '" + name + "' from ArcGIS Online?\n\nThis cannot be undone."
        r = QMessageBox.warning(
            parent, "Delete item", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        item_id = item._meta.get("id", "")
        if not item_id:
            QMessageBox.critical(parent, "Error", "Cannot determine item ID.")
            return
        try:
            item._client.delete_item(item_id)
            if item.parent():
                item.parent().refresh()
        except Exception as e:
            QMessageBox.critical(parent, "Delete failed", str(e))

    def _delete_layer(self, item: AGOLLayerItem):
        """Delete a layer — layers are sub-resources, not directly deletable.
        Inform the user they need to delete the parent service."""
        from qgis.PyQt.QtWidgets import QMessageBox
        parent = self._iface.mainWindow() if self._iface else None
        msg2 = ("Individual layers cannot be deleted separately.\n\n"
                "To remove this layer, delete the parent service item "
                "using right-click \u2192 Delete on the service above it.")
        QMessageBox.information(parent, "Delete layer", msg2)

    def _disconnect(self, item: AGOLConnectionItem):
        self._store.sign_out(item._conn_name)
        item.refresh()

    def _new_connection(self, root: AGOLRootItem):
        from ..dialogs.connection_dialog import ConnectionDialog
        parent = self._iface.mainWindow() if self._iface else None
        dlg = ConnectionDialog(parent=parent)
        if dlg.exec():
            self._store.save_connection(
                dlg.result_name, dlg.result_url,
                username = dlg.entered_username if dlg.save_credentials else "",
                password = dlg.entered_password if dlg.save_credentials else "",
            )
            if dlg.result_client:
                self._store.set_client(dlg.result_name, dlg.result_client)
            root.refresh()

    def _edit_connection(self, item: AGOLConnectionItem):
        from ..dialogs.connection_dialog import ConnectionDialog
        parent = self._iface.mainWindow() if self._iface else None
        dlg = ConnectionDialog(
            name=item._conn_name, url=item._url, parent=parent
        )
        if dlg.exec():
            self._store.save_connection(
                dlg.result_name, dlg.result_url,
                username = dlg.entered_username if dlg.save_credentials else "",
                password = dlg.entered_password if dlg.save_credentials else "",
                old_name = item._conn_name,
            )
            if dlg.result_client:
                self._store.set_client(dlg.result_name, dlg.result_client)
            if item.parent():
                item.parent().refresh()

    def _remove_connection(self, item: AGOLConnectionItem):
        r = QMessageBox.question(
            None, "Remove",
            f"Remove connection '{item._conn_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._store.remove_connection(item._conn_name)
            item.parent().refresh()
