"""
dialogs/browser_dialog.py  —  PyQt6 / QGIS 4.x compatible
"""

import json
import tempfile
import os

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QSplitter, QTextEdit, QMessageBox, QProgressBar,
    QTreeView, QTabWidget, QLabel, QComboBox, QMenu, QAction,
    QAbstractItemView, QFrame,
)
from qgis.PyQt.QtGui import QStandardItemModel, QStandardItem
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel, QPoint

from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsProject, Qgis, QgsApplication
from ..compat import MSG_SUCCESS, MSG_WARNING, MSG_INFO
from qgis.gui import QgsVectorLayerSaveAsDialog

from ..agol_client import AGOLClient, SERVICE_TYPES
from .settings_dialog import SettingsDialog as _Settings
from ..context_menu import build_agol_menu


# ------------------------------------------------------------------ #
#  Worker thread                                                       #
# ------------------------------------------------------------------ #


# ── Module-level helper used by both the dock panel and browser data items ──

def _load_map_or_image_with_dialog(client, svc_root: str,
                                    name: str, service_kind: str):
    """
    Detect what a Map Service or Image Service actually supports,
    then offer the user a choice when multiple options exist.

    Load priority / availability:
      Map Service:
        1. WMS  (MapServer/WMSServer) — live, server-rendered, best quality
        2. XYZ tiles (MapServer/tile/{z}/{y}/{x}) — fast but needs token for CDN
        3. Load as individual feature layers (query each sub-layer)

      Image Service:
        1. WMS  (ImageServer/WMSServer) — if enabled
        2. WCS  (ImageServer/WCSServer) — download raw raster
        3. exportImage — download rendered GeoTIFF for current extent
        4. XYZ tiles  — if tiled image service
    """
    from qgis.PyQt.QtWidgets import QMessageBox, QDialog, QVBoxLayout,         QLabel, QPushButton, QDialogButtonBox
    from qgis.core import QgsRasterLayer, QgsProject

    # ── Detect capabilities ───────────────────────────────────────────
    try:
        caps = client.get_service_capabilities(svc_root)
    except Exception:
        caps = {"wms": False, "wcs": False, "export": False, "tile": False}

    has_wms    = caps.get("wms", False)
    has_wcs    = caps.get("wcs", False)
    has_export = caps.get("export", False)
    is_tiled   = caps.get("tile", service_kind == "map")

    # ── Build list of available options ───────────────────────────────
    options = []
    if has_wms:
        options.append(("WMS (live, server-rendered)", "wms"))
    if is_tiled:
        options.append(("XYZ tiles (cached, fast)", "xyz"))
    if has_wcs and service_kind == "image":
        options.append(("WCS (download raster data)", "wcs"))
    if has_export and service_kind == "image":
        options.append(("Export image for current extent", "export"))
    if service_kind == "map":
        options.append(("Load as feature layers (query each sub-layer)", "features"))

    # ── If only one option, just use it ───────────────────────────────
    if not options:
        # Last resort: try XYZ
        options = [("XYZ tiles (attempt)", "xyz")]

    if len(options) == 1:
        _do_map_load(client, svc_root, name, service_kind, options[0][1])
        return

    # ── Multiple options — show a small picker dialog ─────────────────
    dlg = QDialog()
    dlg.setWindowTitle(f"Load — {name}")
    dlg.setMinimumWidth(380)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(f"<b>{name}</b><br>Choose how to load this service:"))

    chosen = [None]
    for label, key in options:
        btn = QPushButton(label)
        btn.clicked.connect(lambda checked=False, k=key: (chosen.__setitem__(0, k), dlg.accept()))
        lay.addWidget(btn)

    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dlg.reject)
    lay.addWidget(cancel)

    if dlg.exec() and chosen[0]:
        _do_map_load(client, svc_root, name, service_kind, chosen[0])


def _do_map_load(client, svc_root: str, name: str,
                  service_kind: str, method: str):
    """Execute the chosen load method for a map/image service."""
    from qgis.core import QgsRasterLayer, QgsProject, QgsVectorLayer
    from qgis.PyQt.QtWidgets import QMessageBox

    if method == "wms":
        uri = client.get_wms_url(svc_root)
        layer = QgsRasterLayer(uri, name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            QMessageBox.critical(None, "AGOL",
                f"WMS load failed for '{name}'.\n"
                "The WMS extension may be enabled but returning no layers. "
                "Try another load method.")

    elif method == "xyz":
        uri = client.get_xyz_url(svc_root)
        layer = QgsRasterLayer(uri, name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            QMessageBox.critical(None, "AGOL",
                f"XYZ tile load failed for '{name}'.\n"
                "The service may require authentication that the tile CDN "
                "does not accept via URL token. Try WMS or feature layer mode.")

    elif method == "wcs":
        try:
            uri = client.get_wcs_url(svc_root)
            layer = QgsRasterLayer(uri, name, "wcs")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            else:
                QMessageBox.critical(None, "AGOL", f"WCS load failed for '{name}'.")
        except Exception as e:
            QMessageBox.critical(None, "AGOL", f"WCS error: {e}")

    elif method == "export":
        QMessageBox.information(None, "AGOL",
            "Export image mode: pan and zoom to your area of interest in QGIS "
            "first, then this will download a rendered GeoTIFF for that extent.\n\n"
            "This feature will be implemented in a future update.")

    elif method == "features":
        # Load each sub-layer as a vector layer
        try:
            layers = client.get_service_layers(svc_root)
            from .settings_dialog import SettingsDialog as _S
            for lyr in (layers or []):
                lyr_url = f"{svc_root.rstrip('/')}/{lyr['id']}"
                try:
                    geojson = client.query_layer(
                        lyr_url, max_record_count=_S.max_features()
                    )
                    import json, tempfile, os
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".geojson",
                        delete=False, encoding="utf-8"
                    ) as f:
                        json.dump(geojson, f)
                        tmp = f.name
                    vlayer = QgsVectorLayer(tmp, lyr.get("name", name), "ogr")
                    if vlayer.isValid():
                        QgsProject.instance().addMapLayer(vlayer)
                except Exception as e:
                    QMessageBox.warning(None, "AGOL",
                        f"Could not load layer {lyr.get('id', '?')}: {e}")
        except Exception as e:
            QMessageBox.critical(None, "AGOL", f"Error loading sub-layers: {e}")


def _load_layer_from_item(client, layer_url: str,
                           service_kind: str, name: str,
                           item_meta: dict | None = None):
    """
    Load any AGOL layer type into the current QGIS project.
    Populates Layer Properties → Information from AGOL metadata.
    Safe to call from the main thread only.
    """
    import json, tempfile, os
    from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsProject

    if service_kind == "feature":
        try:
            from ..progress_manager import ProgressManager as _PM
            _pm_id = _PM.instance().start(name, "Feature Service")
        except Exception:
            _pm_id = None
        try:
            from .settings_dialog import SettingsDialog as _S
            geojson = client.query_layer(layer_url, max_record_count=_S.max_features())
        except Exception as e:
            if _pm_id:
                try:
                    from ..progress_manager import ProgressManager as _PM2
                    _PM2.instance().fail(_pm_id, str(e))
                except Exception: pass
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(None, "AGOL", str(e))
            return
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp = f.name
        layer = QgsVectorLayer(tmp, name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            try:
                from ..layer_metadata import apply_metadata_async
                apply_metadata_async(layer, item_meta or {}, layer_url, client)
            except Exception:
                pass
            if _pm_id:
                try:
                    from ..progress_manager import ProgressManager as _PM3
                    _PM3.instance().finish(_pm_id, name)
                except Exception: pass
        else:
            try: os.unlink(tmp)
            except Exception: pass
            if _pm_id:
                try:
                    from ..progress_manager import ProgressManager as _PM4
                    _PM4.instance().fail(_pm_id, f"Could not load '{name}'")
                except Exception: pass
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(None, "AGOL", f"Could not load '{name}'")

    elif service_kind in ("map", "image"):
        import re as _re
        svc_root = _re.sub(r"/[0-9]+$", "", layer_url.rstrip("/"))
        _load_map_or_image_with_dialog(client, svc_root, name, service_kind)
        return
    elif service_kind == "tile":
        uri = client.get_xyz_url(layer_url.rsplit("/", 1)[0])
        layer = QgsRasterLayer(uri, name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)




class FetchThread(QThread):
    result   = pyqtSignal(object)
    error    = pyqtSignal(str)
    progress = pyqtSignal(int, int)   # fetched, total

    def __init__(self, fn, *args, progress_kw: str = ""):
        super().__init__()
        self.fn           = fn
        self.args         = args
        self._progress_kw = progress_kw   # kwarg name to inject callback into

    def run(self):
        try:
            if self._progress_kw:
                import inspect
                sig = inspect.signature(self.fn)
                if self._progress_kw in sig.parameters:
                    kwargs = {self._progress_kw: self.progress.emit}
                    self.result.emit(self.fn(*self.args, **kwargs))
                    return
            self.result.emit(self.fn(*self.args))
        except Exception as e:
            try:
                from ..agol_client import AGOLTokenExpiredError
                if isinstance(e, AGOLTokenExpiredError):
                    self.error.emit("__TOKEN_EXPIRED__:" + str(e))
                    return
            except ImportError:
                pass
            self.error.emit(str(e))


# ------------------------------------------------------------------ #
#  Constants  — PyQt6 requires fully-qualified enum access            #
# ------------------------------------------------------------------ #

COL_TITLE    = 0
COL_TYPE     = 1
COL_ACCESS   = 2
COL_OWNER    = 3
COL_MODIFIED = 4
DATA_ROLE    = Qt.ItemDataRole.UserRole + 1   # was Qt.UserRole in PyQt5
_PLACEHOLDER = '__placeholder__'              # sentinel for lazy-load rows

def _lock_icon():
    """Return QIcon for the padlock, or None if file missing."""
    import os as _os
    from qgis.PyQt.QtGui import QIcon as _QI
    p = _os.path.join(_os.path.dirname(__file__), "..", "resources", "icon_lock.png")
    return _QI(p) if _os.path.exists(p) else None



def _make_row(title: str, type_label: str,
              data: dict | None = None,
              owner: str = "", modified: str = "") -> list[QStandardItem]:
    # Extract tags from item meta for display
    tags_str = ""
    if isinstance(data, dict):
        item_meta = data.get("item", {})
        if isinstance(item_meta, dict):
            tags = item_meta.get("tags") or []
            if isinstance(tags, list):
                tags_str = ", ".join(tags[:5])   # show first 5 tags
    a = QStandardItem(title)
    b = QStandardItem(type_label)
    c = QStandardItem(owner)
    d = QStandardItem(modified)
    e = QStandardItem(tags_str)

    # Access column
    access_str = ""
    if isinstance(data, dict):
        item_meta = data.get("item", {})
        if isinstance(item_meta, dict):
            access_str = item_meta.get("access", "")
    ac = QStandardItem(access_str.capitalize() if access_str else "")

    for item in (a, b, ac, c, d, e):
        item.setEditable(False)
    if data is not None:
        a.setData(data, DATA_ROLE)

    # Padlock icon on Type column when not public
    if access_str and access_str != "public":
        icon = _lock_icon()
        if icon:
            b.setIcon(icon)
            b.setToolTip(f"Access: {access_str} — requires sign-in")
        # Also colour the access cell
        from qgis.PyQt.QtGui import QColor, QBrush
        ac.setForeground(QBrush(QColor("#cc6600")))

    return [a, b, ac, c, d, e]


def _placeholder_row() -> list[QStandardItem]:
    ph = QStandardItem("Loading…")
    ph.setData(_PLACEHOLDER, DATA_ROLE)
    ph.setEditable(False)
    return [ph, QStandardItem(""), QStandardItem(""), QStandardItem(""), QStandardItem(""), QStandardItem("")]


def _is_placeholder(item: QStandardItem) -> bool:
    return (item.rowCount() == 1
            and item.child(0).data(DATA_ROLE) == _PLACEHOLDER)


def _make_model() -> QStandardItemModel:
    m = QStandardItemModel(0, 6)
    m.setHorizontalHeaderLabels(["Title / Layer", "Type", "Access", "Owner", "Modified", "Tags"])
    return m



def _fmt_epoch(ms: int) -> str:
    """AGOL epoch-ms → YYYY-MM-DD string."""
    if not ms:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _make_proxy(model: QStandardItemModel) -> QSortFilterProxyModel:
    p = QSortFilterProxyModel()
    p.setSourceModel(model)
    p.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    p.setRecursiveFilteringEnabled(True)
    return p


def _sort_items(items: list, col: int, ascending: bool) -> list:
    """Sort AGOL item dicts by column so ALL rows sort correctly."""
    key_map = {
        0: lambda i: (i.get("title") or "").lower() if isinstance(i, dict) else "",
        1: lambda i: (i.get("type")  or "").lower() if isinstance(i, dict) else "",
        2: lambda i: (i.get("owner") or "").lower() if isinstance(i, dict) else "",
        3: lambda i: (i.get("modified") or 0)       if isinstance(i, dict) else 0,
        4: lambda i: ", ".join(i.get("tags") or []).lower() if isinstance(i, dict) else "",
    }
    key = key_map.get(col, key_map[0])
    return sorted(items, key=key, reverse=not ascending)



def _make_tree(proxy: QSortFilterProxyModel) -> QTreeView:
    t = QTreeView()
    t.setModel(proxy)
    t.setSortingEnabled(True)
    t.setRootIsDecorated(True)
    t.setUniformRowHeights(True)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.header().setStretchLastSection(False)
    t.setColumnWidth(COL_TITLE, 180)
    t.setColumnWidth(COL_TYPE,   80)
    t.setColumnWidth(COL_ACCESS, 60)
    t.setColumnWidth(COL_OWNER,  90)
    t.setColumnWidth(COL_MODIFIED, 80)
    t.setColumnWidth(4, 140)   # Tags
    t.setColumnWidth(2,         90)   # Owner
    t.setColumnWidth(3,         80)   # Modified
    t.header().setStretchLastSection(True)
    t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    return t


# ------------------------------------------------------------------ #
#  Dock panel                                                          #
# ------------------------------------------------------------------ #

class BrowserPanel(QDockWidget):

    def __init__(self, client: AGOLClient, iface):
        super().__init__("AGOL Services", iface.mainWindow())
        self.client = client   # primary client (first connected)
        self.iface  = iface
        self.setObjectName("AGOLBrowserPanel")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea |
            Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._worker: FetchThread | None = None
        self._selection: dict | None = None
        # Map: tab index → {"client": AGOLClient, "name": str,
        #                    "content_model": ..., "search_proxy": ...}
        self._tab_data: dict[int, dict] = {}

        container = QWidget()
        self.setWidget(container)
        self._build_ui(container)

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self, parent: QWidget):
        root = QVBoxLayout(parent)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Custom title bar with separator above "AGOL Services" label
        title_bar = QWidget()
        tb_layout = QVBoxLayout(title_bar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(0)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        tb_layout.addWidget(sep)
        title_lbl = QLabel("AGOL Services")
        title_lbl.setStyleSheet("font-weight: bold; padding: 3px 6px;")
        tb_layout.addWidget(title_lbl)
        self.setTitleBarWidget(title_bar)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # Single flat tab bar: My content | <Connection 1> | <Connection 2> …
        # No inner tabs — search bar is at the top of "My content" tab directly.
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_outer_tab_changed)

        # Build "My content" tab (search bar + folder tree inline)
        self.tabs.addTab(self._build_my_content_tab(), "ArcGIS Living Atlas")

        # Add a tab for each connected service
        self._rebuild_connection_tabs()

        root.addWidget(self.tabs)

        from qgis.PyQt.QtWidgets import QTextBrowser
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setPlaceholderText("Select a service or layer for details.")
        self.detail.setMaximumHeight(80)
        root.addWidget(self.detail)

        # Editable URL row — lets user correct wrong URLs before loading
        url_row = QHBoxLayout()
        url_lbl = QLabel("URL")
        url_lbl.setFixedWidth(30)
        url_lbl.setStyleSheet("color: palette(mid); font-size: 11px;")
        url_row.addWidget(url_lbl)
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("Service URL (editable)")
        self._url_edit.setReadOnly(True)
        self._url_edit.setStyleSheet("font-size: 11px;")
        url_row.addWidget(self._url_edit)
        edit_btn = QPushButton("✎")
        edit_btn.setFixedWidth(26)
        edit_btn.setToolTip("Edit URL before loading")
        edit_btn.clicked.connect(self._toggle_url_edit)
        url_row.addWidget(edit_btn)
        root.addLayout(url_row)
        self._url_edit_mode = False

        btn_row = QHBoxLayout()
        self.load_btn = QPushButton("Add to map")
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self._add_to_map)
        btn_row.addWidget(self.load_btn)

        from qgis.PyQt.QtWidgets import QMenu, QToolButton
        refresh_tb = QToolButton()
        refresh_tb.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        refresh_tb.setToolTip("Refresh (hold for options)")
        refresh_tb.setFixedWidth(28)
        refresh_tb.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        refresh_menu = QMenu(refresh_tb)
        refresh_menu.addAction("Refresh").triggered.connect(self._refresh)
        refresh_menu.addAction(
            "Refresh and re-authenticate…"
        ).triggered.connect(self._refresh_reauth)
        refresh_tb.setMenu(refresh_menu)
        refresh_tb.clicked.connect(self._refresh)
        btn_row.addWidget(refresh_tb)
        root.addLayout(btn_row)

    def _build_my_content_tab(self) -> QWidget:
        """
        ArcGIS Living Atlas tab — search bar + results only.
        No folder tree here; connection tabs handle per-user content.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(3)

        # ── Search bar ────────────────────────────────────────────────
        bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search ArcGIS Online…")
        self.search_edit.returnPressed.connect(self._do_search)
        bar.addWidget(self.search_edit)

        self.type_combo = QComboBox()
        self.type_combo.setFixedWidth(112)
        self.type_combo.addItem("All types",       "")
        self.type_combo.addItem("Feature Service", "Feature Service")
        self.type_combo.addItem("Map Service",     "Map Service")
        self.type_combo.addItem("Image Service",   "Image Service")
        self.type_combo.addItem("Tile Layer",      "Tile Layer")
        self.type_combo.addItem("Web Map",         "Web Map")
        bar.addWidget(self.type_combo)

        search_btn = QPushButton("Search")
        search_btn.setFixedWidth(58)
        search_btn.clicked.connect(self._do_search)
        bar.addWidget(search_btn)

        layout.addLayout(bar)

        # ── Results tree (full height, empty until search runs) ────────
        self.search_model = _make_model()
        self.search_proxy = _make_proxy(self.search_model)
        self.search_tree  = _make_tree(self.search_proxy)
        self.search_tree.clicked.connect(self._on_clicked)
        self.search_tree.expanded.connect(
            lambda idx: self._on_expanded(idx, self.search_model, self.search_proxy)
        )
        self.search_tree.customContextMenuRequested.connect(
            lambda pos: self._show_context_menu(
                pos, self.search_tree, self.search_model, self.search_proxy)
        )
        self.search_tree.header().sectionClicked.connect(self._on_search_header_click)
        self._search_sort_col = 0
        self._search_sort_asc = True
        self._last_search_items: list[dict] = []
        layout.addWidget(self.search_tree, 1)

        # Stub content_model/tree so other methods that reference them don't crash
        self.content_model = _make_model()
        self.content_proxy = _make_proxy(self.content_model)
        self.content_tree  = _make_tree(self.content_proxy)
        return w

    # ------------------------------------------------------------------ #
    #  Search                                                              #
    # ------------------------------------------------------------------ #

    def _do_search(self, owner_only: bool = False):
        from .settings_dialog import SettingsDialog as _S
        query = self.search_edit.text().strip()
        owner = self.client.username if owner_only else ""
        self.search_tree.setVisible(True)
        self._search_limit = _S.search_limit()
        stype = self.type_combo.currentData()
        self._start_worker(
            self.client.search_services, query, owner,
            getattr(self, '_search_limit', 100), stype,
            on_result=self._populate_search,
        )

    def _on_search_header_click(self, col: int):
        """Re-populate model with re-sorted source items on header click."""
        if col == self._search_sort_col:
            self._search_sort_asc = not self._search_sort_asc
        else:
            self._search_sort_col = col
            self._search_sort_asc = True
        if self._last_search_items:
            self._populate_search(
                _sort_items(self._last_search_items,
                            self._search_sort_col,
                            self._search_sort_asc)
            )

    def _populate_search(self, items: list[dict]):
        self._last_search_items = items   # save for re-sort
        items = _sort_items(items, self._search_sort_col, self._search_sort_asc)
        self.search_model.removeRows(0, self.search_model.rowCount())
        self._clear_selection()
        for item in items:
            raw_type = item.get("type", "")
            kind = SERVICE_TYPES.get(raw_type, "")
            row = _make_row(
                item.get("title", "—"), raw_type,
                {"type": kind, "item": item, "raw_type": raw_type},
                owner    = item.get("owner", ""),
                modified = _fmt_epoch(item.get("modified", 0)),
            )
            if kind in ("feature", "map", "image", "webmap"):
                row[0].appendRow(_placeholder_row())
            self.search_model.appendRow(row)

    # ------------------------------------------------------------------ #
    #  My content                                                          #
    # ------------------------------------------------------------------ #

    def _load_my_content(self):
        self._start_worker(
            self.client.get_user_folders, on_result=self._populate_folders
        )

    def _populate_folders(self, folders: list[dict]):
        self.content_model.removeRows(0, self.content_model.rowCount())
        self._clear_selection()
        _folder_icon = QgsApplication.getThemeIcon("/mIconDbSchema.svg")

        root_row = _make_row(
            "Home", "Folder",
            {"type": "folder", "folder_id": "", "title": "Home"},
        )
        root_row[0].setIcon(_folder_icon)
        root_row[0].appendRow(_placeholder_row())
        self.content_model.appendRow(root_row)
        for f in folders:
            row = _make_row(
                f.get("title", "—"), "Folder",
                {"type": "folder", "folder_id": f.get("id", ""),
                 "title": f.get("title", "")},
            )
            row[0].setIcon(_folder_icon)
            row[0].appendRow(_placeholder_row())
            self.content_model.appendRow(row)


    def _load_folder_services(self, parent_item: QStandardItem, folder_id: str):
        self._start_worker(
            self.client.get_folder_items, folder_id,
            on_result=lambda items, pi=parent_item:
                self._add_folder_services(pi, items),
        )

    def _add_folder_services(self, parent_item: QStandardItem,
                              items: list[dict]):
        parent_item.removeRows(0, parent_item.rowCount())
        if not items:
            empty = QStandardItem("(no services)")
            empty.setEditable(False)
            parent_item.appendRow([empty, QStandardItem("")])
            return
        for item in items:
            raw_type = item.get("type", "")
            kind = SERVICE_TYPES.get(raw_type, "")
            row = _make_row(
                item.get("title", "—"), raw_type,
                {"type": kind, "item": item, "raw_type": raw_type},
                owner    = item.get("owner", ""),
                modified = _fmt_epoch(item.get("modified", 0)),
            )
            if kind in ("feature", "map", "image", "webmap"):
                row[0].appendRow(_placeholder_row())
            parent_item.appendRow(row)

    # ------------------------------------------------------------------ #
    #  Expansion                                                           #
    # ------------------------------------------------------------------ #

    def _on_expanded(self, proxy_index, model: QStandardItemModel,
                      proxy: QSortFilterProxyModel):
        src  = proxy.mapToSource(proxy_index)
        item = model.itemFromIndex(src.sibling(src.row(), COL_TITLE))
        if not item or not _is_placeholder(item):
            return
        data = item.data(DATA_ROLE)
        if not data:
            return

        if data["type"] == "folder":
            self._load_folder_services(item, data["folder_id"])
        elif data["type"] == "webmap":
            item_id = data.get("item", {}).get("id", "")
            if item_id:
                self._start_worker(
                    self.client.get_web_map_layers, item_id,
                    on_result=lambda lyrs, pi=item:
                        self._add_webmap_layer_nodes(pi, lyrs),
                )
        elif data["type"] in ("feature", "map", "image"):
            svc_url = data["item"].get("url", "")
            if svc_url:
                self._start_worker(
                    self.client.get_service_layers, svc_url,
                    on_result=lambda lyrs, pi=item, su=svc_url:
                        self._add_layer_nodes(pi, su, lyrs, data["type"]),
                )

    def _add_layer_nodes(self, parent_item: QStandardItem, service_url: str,
                          layers, service_kind: str):
        parent_item.removeRows(0, parent_item.rowCount())
        for lyr in (layers or []):
            layer_url = f"{service_url.rstrip('/')}/{lyr['id']}"
            name = lyr.get("name", "Layer")
            row = _make_row(name, lyr.get("geometryType", "layer"), {
                "type":         "layer",
                "service_kind": service_kind,
                "url":          layer_url,
                "name":         name,
                "meta":         lyr,
            })
            parent_item.appendRow(row)

    def _add_webmap_layer_nodes(self, parent_item, layers):
        """Populate children of a Web Map item with its operational layers."""
        parent_item.removeRows(0, parent_item.rowCount())
        for lyr in (layers or []):
            if not lyr.get("url"):
                continue
            name  = lyr.get("title", "Layer")
            lkind = "feature" if "Feature" in lyr.get("layerType","") else "map"
            row   = _make_row(name, lyr.get("layerType","Layer"), {
                "type":         "layer",
                "service_kind": lkind,
                "url":          lyr["url"],
                "name":         name,
                "meta":         lyr,
            })
            parent_item.appendRow(row)

    # ------------------------------------------------------------------ #
    #  Selection                                                           #
    # ------------------------------------------------------------------ #

    def _on_clicked(self, proxy_index):
        active = self.tabs.currentIndex()
        proxy  = self.search_proxy  if active == 0 else self.content_proxy
        model  = self.search_model  if active == 0 else self.content_model

        src  = proxy.mapToSource(proxy_index)
        item = model.itemFromIndex(src.sibling(src.row(), COL_TITLE))
        if not item:
            return
        data = item.data(DATA_ROLE)
        if not data:
            return

        self._selection = data

        if data["type"] == "folder":
            self.detail.setHtml(f"<b>{data['title']}</b><br>Folder")
            self._url_edit.setText("")
            self.load_btn.setEnabled(False)
        elif data["type"] in ("feature", "map", "image", "tile", "webmap"):
            svc  = data["item"]
            kind = data.get("kind", data["type"])
            url  = svc.get("url", "")

            # Build portal item page URL from item_id (always correct)
            item_id    = svc.get("id", "")
            portal_url = getattr(self.client, "portal_url",
                                 "https://www.arcgis.com").rstrip("/")
            item_page  = (f"{portal_url}/home/item.html?id={item_id}#overview"
                          if item_id else "")

            self.detail.setHtml(
                f"<b>{svc.get('title','—')}</b> "
                f"<span style='color:gray'>[{svc.get('type','')}]</span><br>"
                f"Owner: {svc.get('owner','—')}<br>"
                f"<i>{svc.get('snippet','')}</i><br>"
                + (f'<a href="{item_page}">View item page ↗</a>'
                   if item_page else "")
            )
            self._url_edit.setText(url)
            self._url_edit.setReadOnly(True)
            self._url_edit_mode = False
            self._url_edit.setStyleSheet("font-size: 11px;")
            self.load_btn.setEnabled(True)

            # For image services: fetch capabilities and enrich detail panel
            if kind == "image" and url:
                self._fetch_image_service_detail(url, svc)
        elif data["type"] == "layer":
            self._url_edit.setText(data.get("url", ""))
            self._url_edit.setReadOnly(True)
            self._url_edit_mode = False
            meta = data.get("meta", {})
            self.detail.setHtml(
                f"<b>{data['name']}</b> "
                f"<span style='color:gray'>[{data['service_kind']}]</span><br>"
                f"Geometry: {meta.get('geometryType','—')}<br>"
                f"URL: <a href='{data['url']}'>{data['url']}</a>"
            )
            self.load_btn.setEnabled(True)

    # ------------------------------------------------------------------ #
    #  Image Service                                                       #
    # ------------------------------------------------------------------ #

    def _fetch_image_service_detail(self, svc_url: str, svc_meta: dict):
        """Async fetch ImageServer JSON and update detail panel."""
        def _on_detail(detail):
            exts  = detail.get("supportedExtensions", "") or ""
            caps  = detail.get("capabilities", "") or ""
            bands = detail.get("bandCount", "—")
            ptype = detail.get("pixelType", "—")
            res   = detail.get("pixelSizeX") or detail.get("minScale", "—")
            rows  = detail.get("height", "—")
            cols  = detail.get("width", "—")
            title = svc_meta.get("title", "—")
            url   = svc_meta.get("url", svc_url)

            tick = lambda v: "✔" if v else "✘"
            has_wms  = "WMSServer" in exts
            has_wcs  = "WCSServer" in exts
            has_exp  = "Image" in caps or "Export" in caps

            html = (
                f"<b>{title}</b> "
                f"<span style='color:gray'>[Image Service]</span><br>"
                f"Bands: <b>{bands}</b> &nbsp;|&nbsp; "
                f"Pixel type: <b>{ptype}</b> &nbsp;|&nbsp; "
                f"Size: {cols}×{rows}<br>"
                f"WMS: {tick(has_wms)} &nbsp; "
                f"WCS: {tick(has_wcs)} &nbsp; "
                f"Export: {tick(has_exp)}<br>"
                f"URL: <a href='{url}'>{url}</a>"
            )
            self.detail.setHtml(html)

        w = FetchThread(self.client._get, svc_url.rstrip("/"), {"f": "json"})
        w.result.connect(_on_detail)
        w.error.connect(lambda _: None)
        w.finished.connect(w.deleteLater)
        w.start()
        self._img_detail_worker = w

    def _export_image_for_selection(self):
        """
        Right-click → Export image: download rendered image via
        /ImageServer/exportImage for the current QGIS canvas extent.
        """
        data = self._selection
        if not data:
            return
        kind = data.get("kind", data.get("service_kind", ""))
        if kind != "image":
            return
        svc_url = data.get("item", {}).get("url", data.get("url", ""))
        name    = data.get("item", {}).get("title", data.get("name", "export"))
        if not svc_url:
            return

        # First fetch service JSON to get the native spatial reference
        self.progress.setVisible(True)
        def _fetch_svc_info():
            return self.client._get(svc_url.rstrip("/"), {"f": "json"})

        def _on_svc_info(info):
            # Get native SR from service
            sr = info.get("spatialReference", {})
            if not isinstance(sr, dict):
                sr = {}
            native_wkid = int(sr.get("latestWkid") or sr.get("wkid") or 3857)

            canvas  = self.iface.mapCanvas()
            from qgis.core import (
                QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
            )
            canvas_crs  = canvas.mapSettings().destinationCrs()
            native_crs  = QgsCoordinateReferenceSystem(f"EPSG:{native_wkid}")
            extent      = canvas.extent()

            # Reproject canvas extent to service native CRS
            if canvas_crs != native_crs and native_crs.isValid():
                xf = QgsCoordinateTransform(canvas_crs, native_crs, QgsProject.instance())
                extent = xf.transformBoundingBox(extent)

            bbox = (f"{extent.xMinimum()},{extent.yMinimum()},"
                    f"{extent.xMaximum()},{extent.yMaximum()}")

            # Canvas pixel size — cap at 4096 to avoid huge downloads
            px_w = min(canvas.width()  or 1024, 4096)
            px_h = min(canvas.height() or 768,  4096)

            self._do_export_image(svc_url, bbox, native_wkid, px_w, px_h, name)

        w = FetchThread(_fetch_svc_info)
        w.result.connect(_on_svc_info)
        w.error.connect(lambda e: (self.progress.setVisible(False),
                                   QMessageBox.critical(self, "AGOL", e)))
        w.finished.connect(w.deleteLater)
        w.start()
        self._export_info_worker = w

    def _do_export_image(self, svc_url: str, bbox: str, sr_wkid: int,
                          width: int, height: int, name: str):
        """POST to exportImage and load the result as a QGIS raster layer."""
        import urllib.request, urllib.parse, tempfile, os, json
        from qgis.PyQt.QtWidgets import QMessageBox
        from qgis.core import QgsRasterLayer, QgsProject

        params = {
            "bbox":          bbox,
            "bboxSR":        str(sr_wkid),
            "imageSR":       str(sr_wkid),
            "size":          f"{width},{height}",
            "format":        "tiff",
            "pixelType":     "UNKNOWN",
            "noData":        "",
            "noDataInterpretation": "esriNoDataMatchAny",
            "interpolation": "RSP_BilinearInterpolation",
            "f":             "image",
        }
        if self.client.token:
            params["token"] = self.client.token

        export_url = f"{svc_url.rstrip('/')}/exportImage"
        body = urllib.parse.urlencode(params).encode()

        self.progress.setVisible(True)
        try:
            from ..progress_manager import ProgressManager as _PM
            self._export_pm_id = _PM.instance().start(name, "Image Service export")
        except Exception:
            self._export_pm_id = None

        def _fetch():
            req = urllib.request.Request(
                export_url, data=body,
                headers={
                    "User-Agent":   "QGIS-AGOL-Connector/0.1",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                ct = r.headers.get("Content-Type", "")
                raw = r.read()
            # AGOL returns JSON on error even when f=image
            if "json" in ct or (raw[:1] in (b"{", b"[")):
                try:
                    err = json.loads(raw)
                    raise Exception(
                        err.get("error", {}).get("message", str(err))
                    )
                except (ValueError, AttributeError):
                    pass
            return raw

        def _on_bytes(data_bytes):
            self.progress.setVisible(False)
            suffix = ".tif"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(data_bytes)
                tmp = tf.name
            layer = QgsRasterLayer(tmp, name, "gdal")
            if layer.isValid():
                # Tag with CRS
                from qgis.core import QgsCoordinateReferenceSystem
                crs = QgsCoordinateReferenceSystem(f"EPSG:{sr_wkid}")
                if crs.isValid():
                    layer.setCrs(crs)
                QgsProject.instance().addMapLayer(layer)
            else:
                try: os.unlink(tmp)
                except Exception: pass
                QMessageBox.critical(
                    self, "Export failed",
                    f"Image downloaded ({len(data_bytes):,} bytes) but could "
                    "not be loaded as a raster layer. The service may have "
                    "returned an unsupported image format."
                )

        def _on_err(msg):
            self.progress.setVisible(False)
            QMessageBox.critical(self, "Export failed", msg)

        w = FetchThread(_fetch)
        w.result.connect(_on_bytes)
        w.error.connect(_on_err)
        w.finished.connect(w.deleteLater)
        w.start()
        self._export_worker = w

    def _load_image_as_wms(self, data: dict):
        """Load an Image Service via WMS if the extension is enabled."""
        from qgis.core import QgsRasterLayer, QgsProject
        svc_url = data.get("item", {}).get("url", data.get("url", ""))
        name    = data.get("item", {}).get("title", data.get("name", "layer"))
        if not svc_url:
            return
        try:
            caps = self.client.get_service_capabilities(svc_url)
        except Exception as e:
            QMessageBox.critical(self, "AGOL", f"Could not check capabilities: {e}")
            return
        if not caps.get("wms"):
            QMessageBox.warning(
                self, "WMS not available",
                f"'{name}' does not have the WMS extension enabled.\n\n"
                "Use 'Export image for current extent' instead."
            )
            return
        uri = self.client.get_wms_url(svc_url)
        layer = QgsRasterLayer(uri, name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            QMessageBox.critical(self, "AGOL",
                f"WMS load failed for '{name}'.\n"
                "The WMS extension is enabled but returned no valid layers.")

    def _set_status(self, msg: str):
        pass   # placeholder — progress bar handles status

    # ------------------------------------------------------------------ #
    #  Context menu                                                        #
    # ------------------------------------------------------------------ #

    def _show_context_menu(self, pos: QPoint, tree: QTreeView,
                            model: QStandardItemModel,
                            proxy: QSortFilterProxyModel):
        idx = tree.indexAt(pos)
        if not idx.isValid():
            return
        src_idx = proxy.mapToSource(idx)
        item = model.itemFromIndex(src_idx.sibling(src_idx.row(), COL_TITLE))
        if not item:
            return
        data = item.data(DATA_ROLE)
        if not data or data.get("type") in ("folder", None):
            return

        self._selection = data
        menu = QMenu(tree)

        # Resolve client for this item
        from ..credentials import CredentialStore
        client = CredentialStore.instance().any_client() or self.client

        item_meta = data.get("item", {})
        is_owner  = (item_meta.get("owner", "") == getattr(client, "username", ""))
        item_id   = item_meta.get("id", "") or data.get("item_id", "")

        kind = data.get("kind", data.get("service_kind", data.get("type", "")))
        is_image = (kind == "image")

        build_agol_menu(
            data, menu,
            client        = client,
            on_add_to_map = (None if is_image else self._add_to_map),
            on_add_all    = (self._add_all_to_map
                             if data.get("type") not in ("layer",) and not is_image
                             else None),
            on_save_as    = (self._save_as
                             if (data.get("type") in ("feature", "layer") and
                                 data.get("service_kind", data.get("kind", "")) == "feature")
                             else None),
            on_delete     = (self._delete_selection
                             if (is_owner and item_id and
                                 data.get("type") not in ("layer",))
                             else None),
            parent_widget = self,
        )

        if is_image:
            # Image service: Export image and WMS options
            exp_act = menu.addAction("Export image for current extent…")
            exp_act.triggered.connect(self._export_image_for_selection)
            wms_act = menu.addAction("Load as WMS…")
            wms_act.triggered.connect(lambda: self._load_image_as_wms(data))

        menu.exec(tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ #
    #  Add to map                                                          #
    # ------------------------------------------------------------------ #

    def _add_to_map(self):
        if not self._selection:
            return
        data = self._selection
        kind = data.get("type")

        if kind == "layer":
            svc_kind   = data.get("service_kind", data.get("kind", "feature"))
            item_meta  = data.get("item", {})
            # Use edited URL if user has corrected it
            effective_url = self._url_edit.text().strip() or data.get("url", "")
            if svc_kind == "feature":
                self._add_feature_layer(effective_url, data["name"], item_meta)
            else:
                _load_layer_from_item(
                    self.client, effective_url, svc_kind,
                    data.get("name", "Layer"), item_meta
                )

        elif kind == "webmap":
            item_id = data.get("item_id", "") or data.get("item", {}).get("id", "")
            if item_id:
                try:
                    layers = self.client.get_web_map_layers(item_id)
                    for lyr in (layers or []):
                        if lyr.get("url"):
                            lkind = "feature" if "Feature" in lyr.get("layerType","") else "map"
                            _load_layer_from_item(
                                self.client, lyr["url"], lkind, lyr.get("title","Layer")
                            )
                except Exception as e:
                    self._on_error(str(e))

        elif kind == "feature":
            svc = data["item"]
            self._start_worker(
                self.client.get_service_layers, svc["url"],
                on_result=lambda lyrs: self._load_first_feature_layer(
                    svc["url"], lyrs, svc["title"]),
            )

        elif kind in ("map", "image"):
            svc = data["item"]
            # Check what the service actually supports before picking a strategy
            self._start_worker(
                self.client.get_service_capabilities, svc["url"],
                on_result=lambda caps, s=svc: self._load_raster_by_capability(s, caps),
            )

        elif kind == "tile":
            svc = data["item"]
            uri = self.client.get_xyz_url(svc["url"])
            self._load_raster_layer(uri, svc["title"], "wms")

    def _load_raster_by_capability(self, svc: dict, caps: dict):
        """
        Choose the right QGIS loading strategy based on what the service exposes.

        Priority:
          1. WMS  — live, re-rendered on pan/zoom, best quality
          2. exportImage — download a rendered GeoTIFF for the current map
             extent and add it as a local raster (ImageServer only)
          3. Neither — inform the user clearly

        MapServer without WMS has no good headless download path via the
        public REST API, so we tell the user what's missing.
        """
        title = svc.get("title", "Service")
        url   = svc.get("url", "")
        svc_type = svc.get("type", "")   # "Map Service" / "Image Service"

        # XYZ tiles first — no capability negotiation, works for all hosted services
        try:
            import re as _re
            svc_root = _re.sub(r"/[0-9]+$", "", url.rstrip("/"))
            xyz_uri = self.client.get_xyz_url(svc_root)
            from qgis.core import QgsRasterLayer as _QRL, QgsProject as _QP
            rlay = _QRL(xyz_uri, title, "wms")
            if rlay.isValid():
                _QP.instance().addMapLayer(rlay)
                return
        except Exception:
            pass

        if caps["wms"]:
            uri = self.client.get_wms_url(url)
            self._load_raster_layer(uri, title, "wms")
            return

        if caps["export"] and "Image" in svc_type:
            # No WMS — offer to download the rendered image for the current
            # canvas extent instead
            extent = self.iface.mapCanvas().extent()
            if extent.isEmpty():
                QMessageBox.information(
                    self, "No map extent",
                    f"'{title}' does not expose a WMS endpoint.\n\n"
                    "To download a rendered snapshot, first zoom the map canvas "
                    "to the area you want, then try again."
                )
                return
            bbox = (
                extent.xMinimum(), extent.yMinimum(),
                extent.xMaximum(), extent.yMaximum(),
            )
            self.detail.setHtml(
                self.detail.toHtml() +
                "<br><i>Downloading rendered image for current extent…</i>"
            )
            self._start_worker(
                self.client.export_image_extent, url, bbox,
                on_result=lambda path, t=title: self._load_exported_image(path, t),
            )
            return

        # No usable endpoint
        svc_hint = ""
        if "Map" in svc_type:
            svc_hint = (
                "\n\nThis is a Map Service. WMS must be enabled on the server "
                "by the administrator before it can be loaded into QGIS."
            )
        elif "Image" in svc_type:
            svc_hint = (
                "\n\nThis Image Service has neither WMS nor exportImage "
                "enabled. Contact the service owner."
            )
        QMessageBox.warning(
            self, "Service not loadable",
            f"'{title}' cannot be loaded directly into QGIS.\n"
            f"Supported extensions: {caps['raw'].get('supportedExtensions', 'none')}"
            f"{svc_hint}"
        )

    def _load_exported_image(self, tmp_path: str, name: str):
        """Load a GeoTIFF downloaded via exportImage as a local raster layer."""
        layer = QgsRasterLayer(tmp_path, name)
        if not layer.isValid():
            QMessageBox.critical(self, "Error",
                                 f"Could not load downloaded image for '{name}'.")
            return
        QgsProject.instance().addMapLayer(layer)
        self.iface.messageBar().pushSuccess(
            "AGOL",
            f"Loaded '{name}' as a rendered snapshot. "
            "Re-download to refresh after panning."
        )

    def _load_first_feature_layer(self, svc_url: str,
                                   layers: list[dict], title: str):
        if not layers:
            self.iface.messageBar().pushMessage(
                "AGOL", "No layers found in this service.", MSG_WARNING, 4
            )
            return
        layer_url = f"{svc_url.rstrip('/')}/{layers[0]['id']}"
        item_meta = (self._selection or {}).get("item", {})
        self._add_feature_layer(layer_url, title, item_meta)

    def _add_feature_layer(self, layer_url: str, name: str,
                            item_meta: dict | None = None):
        self.load_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        _max  = _Settings.max_features()
        _meta = item_meta or (self._selection or {}).get("item", {})
        stype = (item_meta or {}).get("type", "Feature Service") if item_meta else "Feature Service"

        try:
            from ..progress_manager import ProgressManager
            _pm_id = ProgressManager.instance().start(name, stype)
        except Exception:
            _pm_id = None

        def _query(url, progress_callback=None):
            return self.client.query_layer(
                url, max_record_count=_max,
                progress_callback=progress_callback,
            )

        def _on_progress(fetched, total):
            self._on_fetch_progress(fetched, total)
            if _pm_id is not None:
                try:
                    from ..progress_manager import ProgressManager
                    ProgressManager.instance().update(_pm_id, fetched, total)
                except Exception:
                    pass

        worker = FetchThread(_query, layer_url, progress_kw="progress_callback")
        worker.progress.connect(_on_progress)
        worker.result.connect(
            lambda gj: self._load_geojson(gj, name, layer_url, _meta)
        )
        worker.result.connect(lambda _: self._reset_progress())
        worker.result.connect(lambda _: _pm_id and self._pm_finish(_pm_id, name))
        worker.error.connect(self._on_error)
        worker.error.connect(lambda e: _pm_id and self._pm_fail(_pm_id, e))
        worker.finished.connect(self._reset_progress)
        self._worker = worker
        self._pm_id  = _pm_id
        worker.start()

    def _toggle_url_edit(self):
        """Toggle URL field between read-only and editable."""
        self._url_edit_mode = not self._url_edit_mode
        self._url_edit.setReadOnly(not self._url_edit_mode)
        if self._url_edit_mode:
            self._url_edit.setStyleSheet(
                "font-size: 11px; background: palette(base);"
            )
            self._url_edit.setFocus()
            self._url_edit.selectAll()
        else:
            self._url_edit.setStyleSheet("font-size: 11px;")
            # Update the selection data with the corrected URL
            if self._selection and isinstance(self._selection, dict):
                self._selection["url"] = self._url_edit.text().strip()

    def _pm_finish(self, task_id, name):
        try:
            from ..progress_manager import ProgressManager
            ProgressManager.instance().finish(task_id, name)
        except Exception:
            pass

    def _pm_fail(self, task_id, msg):
        try:
            from ..progress_manager import ProgressManager
            ProgressManager.instance().fail(task_id, msg)
        except Exception:
            pass

    def _on_fetch_progress(self, fetched: int, total: int):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(fetched)
        # Update status label if present
        if hasattr(self, "detail"):
            pct = int(100 * fetched / total) if total else 0
            # Don't overwrite detail — just use the progress bar

    def _reset_progress(self):
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.load_btn.setEnabled(True)

    def _load_geojson(self, geojson: dict, name: str,
                       layer_url: str = "", item_meta: dict | None = None):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp_path = f.name
        layer = QgsVectorLayer(tmp_path, name, "ogr")
        if not layer.isValid():
            QMessageBox.critical(self, "Error", "Invalid GeoJSON returned.")
            try: os.unlink(tmp_path)
            except Exception: pass
        else:
            QgsProject.instance().addMapLayer(layer)
            # Populate Layer Properties → Information
            from ..layer_metadata import apply_metadata_async
            apply_metadata_async(
                layer,
                item_meta or {},
                layer_url,
                self.client,
            )
            self.iface.messageBar().pushSuccess(
                "AGOL", f"Loaded '{name}' — {layer.featureCount():,} features."
            )
        self.load_btn.setEnabled(True)

    def _load_raster_layer(self, uri: str, name: str, provider: str):
        layer = QgsRasterLayer(uri, name, provider)
        if not layer.isValid():
            QMessageBox.critical(
                self, "Error",
                f"Could not load '{name}'.\n"
                "The service may require a token or the URL may have changed.\n\n"
                f"URI: {uri}"
            )
        else:
            QgsProject.instance().addMapLayer(layer)
            self.iface.messageBar().pushSuccess("AGOL", f"Loaded '{name}'.")

    # ------------------------------------------------------------------ #
    #  Save As                                                             #
    # ------------------------------------------------------------------ #

    def _add_all_to_map(self):
        """Add every sub-layer of the selected service to the map."""
        if not self._selection:
            return
        data = self._selection
        kind = data.get("type") or data.get("kind", "")
        item = data.get("item", {})

        if kind == "webmap":
            item_id = item.get("id", "") or data.get("item_id", "")
            if item_id:
                try:
                    layers = self.client.get_web_map_layers(item_id)
                    for lyr in (layers or []):
                        if lyr.get("url"):
                            lk = "feature" if "Feature" in lyr.get("layerType","") else "map"
                            _load_layer_from_item(
                                self.client, lyr["url"], lk, lyr.get("title","Layer")
                            )
                except Exception as e:
                    self._on_error(str(e))
            return

        svc_url = item.get("url", "") or data.get("url", "")
        if not svc_url:
            return
        try:
            layers = self.client.get_service_layers(svc_url)
        except Exception as e:
            self._on_error(str(e))
            return
        for lyr in (layers or []):
            url = f"{svc_url.rstrip('/')}/{lyr['id']}"
            _load_layer_from_item(self.client, url, kind, lyr.get("name","Layer"))

    def _delete_selection(self):
        """Delete the selected item from AGOL after confirmation."""
        if not self._selection:
            return
        from ..context_menu import _delete_item
        from ..credentials import CredentialStore
        data    = self._selection
        item    = data.get("item", {})
        item_id = item.get("id", "") or data.get("item_id", "")
        name    = item.get("title", data.get("name", "this item"))
        client  = CredentialStore.instance().any_client() or self.client
        if not item_id or not client:
            return

        def _after_delete():
            # Refresh the folder that contained this item
            self._load_my_content()

        from qgis.PyQt.QtWidgets import QMessageBox
        msg = "Permanently delete '" + name + "' from ArcGIS Online?\n\nThis cannot be undone."
        r = QMessageBox.warning(
            self, "Delete item", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            client.delete_item(item_id)
            _after_delete()
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))

    def _save_as(self):
        data = self._selection
        if not data:
            return

        if data["type"] == "layer" and data.get("service_kind") == "feature":
            layer_url = data["url"]
            name = data["name"]
        elif data["type"] == "feature":
            svc_url = data["item"]["url"]
            name    = data["item"]["title"]
            try:
                layers    = self.client.get_service_layers(svc_url)
                layer_url = f"{svc_url.rstrip('/')}/{layers[0]['id']}"
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))
                return
        else:
            return

        self.iface.messageBar().pushMessage(
            "AGOL", "Fetching features for Save As…", MSG_INFO, 3
        )
        _max2 = _Settings.max_features()
        def _q2(url):
            return self.client.query_layer(url, max_record_count=_max2)
        self._start_worker(_q2, layer_url,
                           on_result=lambda gj: self._open_save_as_dialog(gj, name))

    def _open_save_as_dialog(self, geojson: dict, name: str):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp_path = f.name

        layer = QgsVectorLayer(tmp_path, name, "ogr")
        if not layer.isValid():
            QMessageBox.critical(self, "Error", "Failed to load features.")
            try: os.unlink(tmp_path)
            except Exception: pass
            return

        dlg = QgsVectorLayerSaveAsDialog(layer, parent=self)
        if dlg.exec():
            from qgis.core import QgsVectorFileWriter
            opts = dlg.options()
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, dlg.filename(), layer.transformContext(), opts
            )
            if err == QgsVectorFileWriter.WriterError.NoError:
                self.iface.messageBar().pushSuccess(
                    "AGOL", f"Saved '{name}' to {dlg.filename()}"
                )
            else:
                QMessageBox.critical(self, "Save failed", msg)
        try: os.unlink(tmp_path)
        except Exception: pass

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def _refresh(self):
        """Refresh the active tab — re-runs the last search or reloads folders."""
        outer = self.tabs.currentIndex()
        if outer == 0:
            # Living Atlas: re-run last search if there was one, else clear
            if self._last_search_items:
                self._do_search()
            else:
                self.search_model.removeRows(0, self.search_model.rowCount())
        else:
            # Connection tab: reload folder tree, reset loaded flag
            w = self.tabs.widget(outer)
            if hasattr(w, "_model"):
                data = self._tab_data.get(outer, {})
                # Update client in case token was refreshed
                from ..credentials import CredentialStore
                name = data.get("name", "")
                fresh = CredentialStore.instance().get_client(name)
                if fresh:
                    w._client = fresh
                    data["client"] = fresh
                w._model.removeRows(0, w._model.rowCount())
                data["loaded"] = True
                self._load_conn_folders(w)

    def _refresh_reauth(self):
        """Sign out the active connection and force re-authentication."""
        from ..credentials import CredentialStore
        store = CredentialStore.instance()
        outer = self.tabs.currentIndex()
        if outer > 0:
            data = self._tab_data.get(outer, {})
            name = data.get("name", "")
        else:
            name = ""
            for n in store.connection_names():
                if store.get_client(n) is self.client:
                    name = n
                    break
        store.sign_out(name)
        new_client = store.ensure_client(name, parent=self)
        if new_client:
            if outer > 0:
                self._tab_data[outer]["client"] = new_client
                self._tab_data[outer]["loaded"] = False
            else:
                self.client = new_client
            self._refresh()

    # ── Connection tabs ────────────────────────────────────────────────

    def _rebuild_connection_tabs(self):
        """
        Rebuild per-connection tabs to reflect current sign-in state.
        Called on panel open and after sign-in/out.
        """
        from ..credentials import CredentialStore
        store = CredentialStore.instance()

        # Remove all connection tabs (keep tab 0 = My content)
        while self.tabs.count() > 1:
            self.tabs.removeTab(1)
        self._tab_data = {}

        for name in store.connection_names():
            client = store.get_client(name)
            if not client:
                continue
            tab_idx = self.tabs.count()
            tab_widget = self._build_connection_tab(name, client)
            self.tabs.addTab(tab_widget, name)
            self._tab_data[tab_idx] = {
                "name":          name,
                "client":        client,
                "loaded":        False,
            }

    def _build_connection_tab(self, conn_name: str,
                               client: AGOLClient) -> QWidget:
        """Build a folder-tree panel for a single connection."""
        from qgis.core import QgsApplication
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 4, 2, 0)
        v.setSpacing(2)

        # ── Header: icon + "Name — signed in as user" ────────────────
        # Matches the look in the screenshot
        hdr = QHBoxLayout()
        hdr.setSpacing(4)

        conn_icon = QLabel()
        conn_icon.setPixmap(
            QgsApplication.getThemeIcon("/mIconConnect.svg").pixmap(16, 16)
        )
        hdr.addWidget(conn_icon)

        hdr_lbl = QLabel(
            f"<b>{conn_name}</b>"
            f"<span style='color:palette(mid)'> — signed in as {client.username}</span>"
        )
        hdr_lbl.setTextFormat(Qt.TextFormat.RichText)
        hdr.addWidget(hdr_lbl)
        hdr.addStretch()
        v.addLayout(hdr)

        # Thin separator line
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")
        v.addWidget(line)

        # ── Search bar for this connection ───────────────────────────
        conn_bar = QHBoxLayout()
        conn_search = QLineEdit()
        conn_search.setPlaceholderText(f"Search {conn_name}…")
        conn_bar.addWidget(conn_search)

        conn_type = QComboBox()
        conn_type.setFixedWidth(112)
        conn_type.addItem("All types",       "")
        conn_type.addItem("Feature Service", "Feature Service")
        conn_type.addItem("Map Service",     "Map Service")
        conn_type.addItem("Image Service",   "Image Service")
        conn_type.addItem("Tile Layer",      "Tile Layer")
        conn_type.addItem("Web Map",         "Web Map")
        conn_bar.addWidget(conn_type)

        conn_search_btn = QPushButton("Search")
        conn_search_btn.setFixedWidth(58)
        conn_bar.addWidget(conn_search_btn)
        v.addLayout(conn_bar)

        # Search results tree (hidden until search runs)
        search_model = _make_model()
        search_proxy = _make_proxy(search_model)
        search_tree  = _make_tree(search_proxy)
        search_tree.setVisible(False)
        v.addWidget(search_tree)

        # Wire search for this connection
        def _conn_search(_c=client, _sm=search_model, _st=search_tree,
                          _se=conn_search, _tc=conn_type):
            q = _se.text().strip()
            stype = _tc.currentData() or ""
            if not q:
                _st.setVisible(False)
                return
            _st.setVisible(True)
            _sm.removeRows(0, _sm.rowCount())

            def _on_results(items):
                _sm.removeRows(0, _sm.rowCount())
                for itm in items:
                    raw_type = itm.get("type", "")
                    kind = SERVICE_TYPES.get(raw_type, "feature")
                    row = _make_row(
                        itm.get("title", "—"), raw_type,
                        {"type": "service", "item": itm, "kind": kind,
                         "url": itm.get("url", ""),
                         "name": itm.get("title", "—")}
                    )
                    import os as _os3
                    _rdir3 = _os3.path.join(_os3.path.dirname(__file__), "..", "resources")
                    from qgis.PyQt.QtGui import QIcon as _QI3
                    _icons3 = {
                        "feature": "icon_feature_service.png",
                        "map":     "icon_map_service.png",
                        "image":   "icon_image_service.png",
                        "tile":    "icon_tile_service.png",
                        "vtile":   "icon_vtile_service.png",
                        "webmap":  "icon_webmap.png",
                    }
                    row[0].setIcon(_QI3(_os3.path.join(_rdir3, _icons3.get(kind, "icon_feature_service.png"))))
                    row[0].appendRow(_placeholder_row())
                    _sm.appendRow(row)

            from .settings_dialog import SettingsDialog as _S2
            w2 = FetchThread(_c.search_services, q, "", _S2.search_limit(), stype)
            w2.result.connect(_on_results)
            w2.finished.connect(w2.deleteLater)
            w2.start()

        conn_search_btn.clicked.connect(_conn_search)
        conn_search.returnPressed.connect(_conn_search)

        # ── Folder / service tree ─────────────────────────────────────
        tree = QTreeView()
        tree.setHeaderHidden(False)
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(True)
        tree.setAnimated(False)
        tree.setWordWrap(False)
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Name", "Type", "Access", "Owner", "Modified", "Tags"])
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(model)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        proxy.setFilterKeyColumn(0)
        tree.setModel(proxy)
        tree.setColumnWidth(0, 240)
        tree.setColumnWidth(1, 80)
        tree.setColumnWidth(2, 90)
        tree.setColumnWidth(3, 80)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        tree.doubleClicked.connect(
            lambda idx, t=tree, p=proxy, m=model, c=client:
            self._on_conn_tree_double_click(idx, t, p, m, c)
        )
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree, p=proxy, m=model, c=client:
            self._show_context_menu(pos, t, m, p)
        )
        tree.expanded.connect(
            lambda idx, t=tree, p=proxy, m=model, c=client:
            self._on_conn_tree_expanded(idx, t, p, m, c)
        )
        tree.selectionModel().selectionChanged.connect(
            lambda sel, desel, t=tree, p=proxy, m=model:
            self._on_conn_tree_selection(t, p, m)
        )

        v.addWidget(tree, 1)

        # Store refs on the widget for later access
        w._tree   = tree
        w._model  = model
        w._proxy  = proxy
        w._client = client
        w._name   = conn_name
        return w

    def _on_outer_tab_changed(self, index: int):
        """Load connection content on first visit to that tab."""
        if index == 0:
            self.client = self.client   # My content — no change
            return
        data = self._tab_data.get(index)
        if data and not data["loaded"]:
            data["loaded"] = True
            self.client = data["client"]
            w = self.tabs.widget(index)
            if hasattr(w, "_model"):
                self._load_conn_folders(w)

    def _load_conn_folders(self, tab_widget: QWidget):
        """Populate a connection tab's tree with Home + user folders."""
        client = tab_widget._client
        model  = tab_widget._model
        proxy  = tab_widget._proxy

        self.progress.setVisible(True)
        model.removeRows(0, model.rowCount())

        def _on_folders(folders):
            self.progress.setVisible(False)
            model.removeRows(0, model.rowCount())
            from qgis.core import QgsApplication
            import os as _os
            _rdir = _os.path.join(_os.path.dirname(__file__), "..", "resources")
            from qgis.PyQt.QtGui import QIcon as _QIcon
            folder_icon = _QIcon(_os.path.join(_rdir, "icon_folder.png"))
            # Home + user folders
            all_folders = [("Home", "")] + [
                (f.get("title", "—"), f.get("id", "")) for f in folders
            ]
            for folder_title, folder_id in all_folders:
                row = _make_row(folder_title, "Folder",
                                {"type": "folder", "folder_id": folder_id,
                                 "title": folder_title})
                row[0].setIcon(folder_icon)
                # Clear Type column for folders — matches screenshot
                row[1].setText("")
                row[0].appendRow(_placeholder_row())
                model.appendRow(row)

        w = FetchThread(client.get_user_folders)
        w.result.connect(_on_folders)
        w.error.connect(lambda e: (self.progress.setVisible(False),
                                   self._on_error(e)))
        w.finished.connect(w.deleteLater)
        w.start()
        self._conn_folder_worker = w

    def _on_conn_tree_expanded(self, index, tree, proxy, model, client):
        """Lazy-load folder or service contents in a connection tab."""
        src_idx = proxy.mapToSource(index)
        item = model.itemFromIndex(src_idx)
        if not item:
            return
        data = item.data(DATA_ROLE)
        if not data or not isinstance(data, dict):
            return

        node_type = data.get("type", "")

        # ── Service node → load its layers ────────────────────────────
        if node_type == "service":
            if not (item.rowCount() == 1 and
                    item.child(0).data(DATA_ROLE) == _PLACEHOLDER):
                return
            item.removeRows(0, item.rowCount())
            self.progress.setVisible(True)
            svc_item = data.get("item", {})
            kind     = data.get("kind", "feature")
            svc_url  = svc_item.get("url", data.get("url", ""))
            item_id  = svc_item.get("id", "")

            if kind == "webmap" and item_id:
                def _on_wm_layers(layers, it=item, meta=svc_item):
                    self.progress.setVisible(False)
                    it.removeRows(0, it.rowCount())
                    for lyr in (layers or []):
                        if not lyr.get("url"):
                            continue
                        lkind = "feature" if "Feature" in lyr.get("layerType","") else "map"
                        row = _make_row(
                            lyr.get("title","Layer"), lyr.get("layerType",""),
                            {"type":"layer","url":lyr["url"],
                             "name":lyr.get("title","Layer"),
                             "kind":lkind,"service_kind":lkind,"item":meta}
                        )
                        it.appendRow(row)
                w = FetchThread(client.get_web_map_layers, item_id)
                w.result.connect(_on_wm_layers)
                w.error.connect(lambda e: self.progress.setVisible(False))
                w.finished.connect(w.deleteLater)
                w.start()
                self._conn_svc_worker = w
            elif svc_url:
                def _on_layers(layers, it=item, meta=svc_item, k=kind, url=svc_url):
                    self.progress.setVisible(False)
                    it.removeRows(0, it.rowCount())
                    import os as _os3
                    _rdir3 = _os3.path.join(_os3.path.dirname(__file__),"..", "resources")
                    from qgis.PyQt.QtGui import QIcon as _QI3
                    _licons = {
                        "esriGeometryPoint":      "icon_point.png",
                        "esriGeometryPolyline":   "icon_line.png",
                        "esriGeometryPolygon":    "icon_polygon.png",
                        "esriGeometryMultiPatch": "icon_polygon.png",
                    }
                    for lyr in (layers or []):
                        lyr_url = f"{url.rstrip('/')}/{lyr['id']}"
                        row = _make_row(
                            lyr.get("name", f"Layer {lyr['id']}"), "",
                            {"type":"layer","url":lyr_url,
                             "name":lyr.get("name","Layer"),
                             "kind":k,"service_kind":k,"item":meta}
                        )
                        geom = lyr.get("geometryType","")
                        icon_file = _licons.get(geom, "icon_polygon.png") if k=="feature" else "icon_map_service.png"
                        row[0].setIcon(_QI3(_os3.path.join(_rdir3, icon_file)))
                        it.appendRow(row)
                w = FetchThread(client.get_service_layers, svc_url)
                w.result.connect(_on_layers)
                w.error.connect(lambda e: self.progress.setVisible(False))
                w.finished.connect(w.deleteLater)
                w.start()
                self._conn_svc_worker = w
            else:
                self.progress.setVisible(False)
                item.removeRows(0, item.rowCount())
            return

        # ── Folder node → load its services ───────────────────────────
        if node_type != "folder":
            return
        if (item.rowCount() == 1 and
                item.child(0).data(DATA_ROLE) == _PLACEHOLDER):
            item.removeRows(0, item.rowCount())
            self.progress.setVisible(True)
            folder_id = data.get("folder_id", "")

            def _on_items(items, it=item):
                self.progress.setVisible(False)
                it.removeRows(0, it.rowCount())
                from qgis.core import QgsApplication as _QA
                import os as _os2
                _rdir2 = _os2.path.join(_os2.path.dirname(__file__), "..", "resources")
                from qgis.PyQt.QtGui import QIcon as _QIcon2
                def _svc_icon(name):
                    return _QIcon2(_os2.path.join(_rdir2, name))
                _kind_icons = {
                    "feature": "icon_feature_service.png",
                    "map":     "icon_map_service.png",
                    "image":   "icon_image_service.png",
                    "tile":    "icon_tile_service.png",
                    "vtile":   "icon_vtile_service.png",
                    "webmap":  "icon_webmap.png",
                }
                for svc in items:
                    raw_type = svc.get("type", "")
                    kind = SERVICE_TYPES.get(raw_type, "feature")
                    row = _make_row(
                        svc.get("title", "—"), raw_type,
                        {"type": "service", "item": svc, "kind": kind,
                         "url": svc.get("url", ""),
                         "name": svc.get("title", "—")}
                    )
                    icon_name = _kind_icons.get(kind, "icon_feature_service.png")
                    row[0].setIcon(_svc_icon(icon_name))
                    row[0].appendRow(_placeholder_row())
                    it.appendRow(row)

            w = FetchThread(client.get_folder_items, folder_id)
            w.result.connect(_on_items)
            w.error.connect(lambda e: self.progress.setVisible(False))
            w.finished.connect(w.deleteLater)
            w.start()
            self._conn_item_worker = w

    def _on_conn_tree_double_click(self, index, tree, proxy, model, client):
        """Double-click in a connection tab — add layer to map."""
        src_idx = proxy.mapToSource(index)
        item = model.itemFromIndex(src_idx)
        if not item:
            return
        data = item.data(DATA_ROLE)
        if not data:
            return
        # Temporarily set self.client for the load call
        old_client = self.client
        self.client = client
        self._selection = data
        self._add_to_map()
        self.client = old_client

    def _on_conn_tree_selection(self, tree, proxy, model):
        """Update detail pane and Add button on selection change."""
        indexes = tree.selectionModel().selectedIndexes()
        if not indexes:
            self._clear_selection()
            return
        src_idx = proxy.mapToSource(indexes[0])
        item = model.itemFromIndex(src_idx)
        if not item:
            self._clear_selection()
            return
        data = item.data(DATA_ROLE)
        # Guard: placeholder sentinel is a string, not a dict
        if not data or not isinstance(data, dict):
            self._clear_selection()
            return
        if data.get("type") == "folder":
            self._clear_selection()
            return
        self._selection = data
        self.load_btn.setEnabled(True)

    def show_and_refresh(self):
        self.show()
        self.raise_()
        self._rebuild_connection_tabs()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _clear_selection(self):
        self._selection = None
        self.load_btn.setEnabled(False)

    def _start_worker(self, fn, *args, on_result=None):
        self.progress.setVisible(True)
        self._worker = FetchThread(fn, *args)
        if on_result:
            self._worker.result.connect(on_result)
        self._worker.result.connect(lambda _: self.progress.setVisible(False))
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self.progress.setVisible(False))
        self._worker.start()

    def _on_error(self, msg: str):
        self.progress.setVisible(False)
        self.load_btn.setEnabled(True)
        if msg.startswith("__TOKEN_EXPIRED__:"):
            # Silently re-authenticate and automatically retry the refresh
            from ..credentials import CredentialStore
            store = CredentialStore.instance()
            name  = ""
            for n in store.connection_names():
                if store.get_client(n) is self.client:
                    name = n
                    break
            new_client = store.handle_token_expiry(name, parent=None)
            if new_client:
                self.client = new_client
                # Auto-retry: refresh the current view silently
                self._refresh()
            else:
                # Could not re-auth silently — prompt
                new_client2 = store.ensure_client(name, parent=self)
                if new_client2:
                    self.client = new_client2
                    self._refresh()
            return
        QMessageBox.critical(self, "Request failed", msg)
