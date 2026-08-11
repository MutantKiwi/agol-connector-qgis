"""
provider/agol_source_select.py
==============================
Data Source Manager integration — adds an "AGOL" tab.

Shows:
  Left:  connection list + New / Edit / Remove / Connect buttons
  Right: folder tree → services → layers (same tree as browser panel)
         with Add button at the bottom

Registered via QgsGui.sourceSelectProviderRegistry().addProvider().
"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QSplitter, QTreeWidget,
    QTreeWidgetItem, QLabel, QProgressBar, QMessageBox,
    QAbstractItemView, QMenu, QAction, QComboBox,
    QLineEdit, QCheckBox, QFrame, QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtGui import QDesktopServices

from qgis.gui import QgsAbstractDataSourceWidget, QgsSourceSelectProvider
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer, Qgis, QgsApplication

from ..agol_client import AGOLClient, SERVICE_TYPES
from ..credentials import CredentialStore as _CS
from ..context_menu import build_agol_menu


# ── Worker ─────────────────────────────────────────────────────────────────

class Worker(QThread):
    result = pyqtSignal(object)
    error  = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.result.emit(self.fn(*self.args))
        except Exception as e:
            self.error.emit(str(e))


# ── Data Source Manager widget ─────────────────────────────────────────────

DATA_ROLE = Qt.ItemDataRole.UserRole + 1
_PH = "__placeholder__"


def _fmt_epoch(ms: int) -> str:
    """AGOL epoch-ms → YYYY-MM-DD string."""
    if not ms:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


class _NullButton:
    """No-op stub replacing the removed add_btn so existing code doesn't crash."""
    def setEnabled(self, *a): pass
    def setText(self, *a): pass
    def isEnabled(self): return False


class _ComboListProxy:
    """Proxy so any remaining conn_list references work after switch to QComboBox."""
    def __init__(self, combo):
        self._c = combo
    def currentRow(self): return self._c.currentIndex()
    def currentItem(self):
        t = self._c.currentText()
        if not t: return None
        class _I:
            def __init__(self, t): self._t = t
            def text(self): return self._t
        return _I(t)
    def addItem(self, *a): pass
    def clear(self): pass
    def count(self): return self._c.count()


class AGOLSourceSelectWidget(QgsAbstractDataSourceWidget):

    def __init__(self, parent=None, fl=Qt.WindowType.Widget,
                 widgetMode=0):
        super().__init__(parent, fl, widgetMode)
        self._worker: Worker | None = None
        self._active_client: AGOLClient | None = None
        self._build_ui()
        # Now that _build_ui ran, replace stub with real button
        self.add_btn = self._add_btn2
        self._refresh_connections()
        # Also wire DSM framework addLayer signal if available
        try:
            self.addLayer.connect(self._add_layer)
        except Exception:
            pass

    # ── UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        """
        Layout matches the built-in ArcGIS REST Server tab:
          Row 1: connection dropdown (full width)
          Row 2: Connect | New | Edit | Remove | Refresh     Load | Save
          Row 3: search field
          Row 4: tree (stretch)
          Row 5: extent checkbox
          Row 6: thin progress bar
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── Row 1: Connection label + dropdown ─────────────────────────
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Server connections"))
        conn_row.addSpacing(6)

        self.conn_combo = QComboBox()
        self.conn_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.conn_combo.currentIndexChanged.connect(self._on_combo_changed)
        conn_row.addWidget(self.conn_combo, 1)
        self.conn_list = _ComboListProxy(self.conn_combo)
        # add_btn alias set after buttons are created in _build_ui
        self.add_btn = _NullButton()  # replaced after _build_ui

        root.addLayout(conn_row)

        # ── Row 2: Action buttons ──────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self.con_btn  = QPushButton("Connect")
        self.new_btn  = QPushButton("New")
        self.edit_btn = QPushButton("Edit")
        self.del_btn  = QPushButton("Remove")
        self.ref_btn  = QPushButton("Refresh")

        for b in (self.con_btn, self.new_btn, self.edit_btn,
                  self.del_btn, self.ref_btn):
            btn_row.addWidget(b)

        btn_row.addStretch(1)

        self.load_cfg_btn = QPushButton("Load")
        self.save_cfg_btn = QPushButton("Save")
        btn_row.addWidget(self.load_cfg_btn)
        btn_row.addWidget(self.save_cfg_btn)

        self.con_btn.clicked.connect(self._connect)
        self.new_btn.clicked.connect(self._new_connection)
        self.edit_btn.clicked.connect(self._edit_connection)
        self.del_btn.clicked.connect(self._remove_connection)
        self.ref_btn.clicked.connect(self._refresh_tree)
        self.load_cfg_btn.clicked.connect(self._load_connections_file)
        self.save_cfg_btn.clicked.connect(self._save_connections_file)
        root.addLayout(btn_row)

        # ── Row 3: Search ──────────────────────────────────────────────
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search)
        root.addWidget(self.search_edit)

        # ── Row 4: Tree ────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Owner", "Modified"])
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 280)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 90)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.currentItemChanged.connect(self._on_selection_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self.tree, 1)

        # ── Row 5: Options ─────────────────────────────────────────────
        self.extent_cb = QCheckBox(
            "Only request features overlapping the current view extent"
        )
        root.addWidget(self.extent_cb)

        # ── Row 6: Progress bar (thin, hidden until loading) ───────────
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # ── Row 7: Web Service link ─────────────────────────────────────
        from qgis.PyQt.QtWidgets import QGroupBox
        url_box = QGroupBox("Web service link")
        url_layout = QVBoxLayout(url_box)
        url_layout.setContentsMargins(6, 4, 6, 4)
        self._url_label = QLabel("")
        self._url_label.setWordWrap(True)
        self._url_label.setOpenExternalLinks(True)
        self._url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self._url_label.setStyleSheet("font-size: 11px;")
        url_layout.addWidget(self._url_label)
        root.addWidget(url_box)

        # ── Row 8: Coordinate Reference System ─────────────────────────
        crs_box = QGroupBox("Coordinate Reference System")
        crs_layout = QVBoxLayout(crs_box)
        crs_layout.setContentsMargins(6, 4, 6, 4)
        self._crs_label = QLabel("")
        self._crs_label.setStyleSheet("font-size: 11px;")
        self._crs_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        crs_layout.addWidget(self._crs_label)
        root.addWidget(crs_box)

        # ── Row 9: Action buttons (matches QGIS DSM style) ─────────────
        from qgis.PyQt.QtWidgets import QDialogButtonBox
        action_row = QHBoxLayout()

        action_row.addStretch(1)

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self._close_panel)
        action_row.addWidget(self._close_btn)

        self._add_filter_btn = QPushButton("Add with Filter")
        self._add_filter_btn.setEnabled(False)
        self._add_filter_btn.setToolTip(
            "Add selected layer with a feature filter applied"
        )
        self._add_filter_btn.clicked.connect(self._add_layer_with_filter)
        action_row.addWidget(self._add_filter_btn)

        self._add_btn2 = QPushButton("Add")
        self._add_btn2.setDefault(True)
        self._add_btn2.setEnabled(False)
        self._add_btn2.clicked.connect(self._add_layer)
        action_row.addWidget(self._add_btn2)

        help_btn = QPushButton("Help")
        help_btn.clicked.connect(self._open_help)
        action_row.addWidget(help_btn)

        root.addLayout(action_row)



    # ── Connection management ──────────────────────────────────────────

    def _refresh_connections(self):
        self.conn_combo.blockSignals(True)
        current = self.conn_combo.currentText()
        self.conn_combo.clear()
        for name in _CS.instance().connection_names():
            self.conn_combo.addItem(name)
        idx = self.conn_combo.findText(current)
        self.conn_combo.setCurrentIndex(max(0, idx))
        self.conn_combo.blockSignals(False)
        self._update_buttons()

    def _update_buttons(self):
        has_sel = bool(self.conn_combo.currentText())
        self.edit_btn.setEnabled(has_sel)
        self.del_btn.setEnabled(has_sel)
        name = self._current_name()
        connected = bool(name and _CS.instance().is_signed_in(name))
        self.con_btn.setText("Disconnect" if connected else "Connect")

    def _current_name(self) -> str:
        return self.conn_combo.currentText()

    def _on_combo_changed(self, index: int):
        self._update_buttons()
        name = self.conn_combo.currentText()
        if name and _CS.instance().is_signed_in(name):
            self.tree.clear()
            self.add_btn.setEnabled(False)
            self._load_connection(name)
        else:
            self.tree.clear()
            self.add_btn.setEnabled(False)

    def _on_connection_changed(self, row: int):
        self._on_combo_changed(row)


    def _new_connection(self):
        from ..dialogs.connection_dialog import ConnectionDialog
        dlg = ConnectionDialog(parent=self)
        if dlg.exec():
            _CS.instance().save_connection(
                dlg.result_name, dlg.result_url,
                username=dlg.entered_username if dlg.save_credentials else "",
                password=dlg.entered_password if dlg.save_credentials else "",
            )
            if dlg.result_client:
                _CS.instance().set_client(dlg.result_name, dlg.result_client)
            self._refresh_connections()
            if dlg.result_client:
                for i in range(self.conn_list.count()):
                    if self.conn_list.item(i).text() == dlg.result_name:
                        self.conn_list.setCurrentRow(i)
                        break

    def _edit_connection(self):
        name = self._current_name()
        if not name:
            return
        from ..dialogs.connection_dialog import ConnectionDialog
        dlg = ConnectionDialog(
            name=name, url=_CS.instance().connection_url(name), parent=self
        )
        if dlg.exec():
            _CS.instance().save_connection(
                dlg.result_name, dlg.result_url,
                username=dlg.entered_username if dlg.save_credentials else "",
                password=dlg.entered_password if dlg.save_credentials else "",
                old_name=name,
            )
            if dlg.result_client:
                _CS.instance().set_client(dlg.result_name, dlg.result_client)
            self._refresh_connections()

    def _remove_connection(self):
        name = self._current_name()
        if not name:
            return
        r = QMessageBox.question(
            self, "Remove", f"Remove '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            _CS.instance().remove_connection(name)
            self._refresh_connections()
            self.tree.clear()

    def _connect(self):
        name = self._current_name()
        if not name:
            return
        store = _CS.instance()
        if store.is_signed_in(name):
            store.sign_out(name)
            self._update_buttons()
            self.tree.clear()
            return
        client = store.ensure_client(name, parent=self)
        if client:
            self._update_buttons()
            self._load_connection(name)



    def _load_connection(self, name: str):
        self.tree.clear()
        self.add_btn.setEnabled(False)
        client = _CS.instance().get_client(name)
        if not client:
            return
        self._active_client = client
        self._run(client.get_user_folders, on_result=self._populate_folders)

    def _populate_folders(self, folders: list[dict]):
        self.tree.clear()
        client = self._active_client

        import os as _fos
        _frdir = _fos.path.join(_fos.path.dirname(__file__), "..", "resources")
        from qgis.PyQt.QtGui import QIcon as _FIcon
        _folder_icon = _FIcon(_fos.path.join(_frdir, "icon_folder.png"))

        def _add_folder(title: str, folder_id: str):
            node = QTreeWidgetItem([title, "Folder", "", ""])
            node.setData(0, DATA_ROLE,
                         {"type": "folder", "folder_id": folder_id})
            node.setIcon(0, _folder_icon)
            ph = QTreeWidgetItem(["Loading…", "", "", ""])
            ph.setData(0, DATA_ROLE, _PH)
            node.addChild(ph)
            self.tree.addTopLevelItem(node)

        _add_folder("Home", "")
        for f in folders:
            _add_folder(f.get("title", "—"), f.get("id", ""))

    def _on_item_expanded(self, node: QTreeWidgetItem):
        data = node.data(0, DATA_ROLE)
        if not data or data == _PH:
            return
        # Only expand if still showing placeholder
        if node.childCount() == 1 and \
                node.child(0).data(0, DATA_ROLE) == _PH:
            if data.get("type") == "folder":
                self._load_folder(node, data["folder_id"])
            elif data.get("type") == "service":
                item = data["item"]
                if data.get("kind") == "webmap":
                    self._load_webmap_layers(node, item)
                else:
                    self._load_service_layers(node, item)

    def _load_folder(self, node: QTreeWidgetItem, folder_id: str):
        client = self._active_client
        if not client:
            return
        self._run(
            client.get_folder_items, folder_id,
            on_result=lambda items, n=node: self._add_service_nodes(n, items),
        )

    def _add_service_nodes(self, node: QTreeWidgetItem, items: list[dict]):
        node.takeChildren()
        for item in items:
            raw_type = item.get("type", "")
            owner    = item.get("owner", "")
            modified = _fmt_epoch(item.get("modified", 0))
            child = QTreeWidgetItem([item.get("title", "—"), raw_type, owner, modified])
            child.setData(0, DATA_ROLE,
                          {"type": "service", "item": item,
                           "kind": SERVICE_TYPES.get(raw_type, "feature")})
            ph = QTreeWidgetItem(["Loading…", "", "", ""])
            ph.setData(0, DATA_ROLE, _PH)
            child.addChild(ph)
            node.addChild(child)

    def _load_service_layers(self, node: QTreeWidgetItem, item: dict):
        client = self._active_client
        if not client:
            return
        svc_url = item.get("url", "")
        if not svc_url:
            return
        self._run(
            client.get_service_layers, svc_url,
            on_result=lambda lyrs, n=node, i=item: (
                self._add_layer_nodes(n, i, lyrs)
            ),
        )

    def _load_webmap_layers(self, node: QTreeWidgetItem, item: dict):
        """Expand a Web Map node by fetching its operational layers."""
        client = self._active_client
        if not client:
            return
        item_id = item.get("id", "")
        if not item_id:
            node.takeChildren()
            node.addChild(QTreeWidgetItem(["No item ID available", "", "", ""]))
            return
        self._run(
            client.get_web_map_layers, item_id,
            on_result=lambda lyrs, n=node, i=item: self._add_webmap_layer_nodes(n, i, lyrs),
        )

    def _add_webmap_layer_nodes(self, node: QTreeWidgetItem,
                                 item: dict, layers: list[dict]):
        """Populate Web Map children with its operational layers."""
        node.takeChildren()
        if not layers:
            node.addChild(QTreeWidgetItem(["No layers found", "", "", ""]))
            return
        for lyr in layers:
            url   = lyr.get("url", "")
            name  = lyr.get("title", "Layer")
            ltype = lyr.get("layerType", "")
            kind  = "feature" if "Feature" in ltype else "map"
            child = QTreeWidgetItem([name, ltype or "Layer", "", ""])
            child.setData(0, DATA_ROLE, {
                "type": "layer", "url": url, "name": name,
                "kind": kind, "service_kind": kind, "item": item,
            })
            node.addChild(child)


    def _add_layer_nodes(self, node: QTreeWidgetItem,
                          item: dict, layers: list[dict]):
        node.takeChildren()
        svc_url = item.get("url", "")
        kind    = SERVICE_TYPES.get(item.get("type", ""), "feature")
        for lyr in layers:
            layer_url = f"{svc_url.rstrip('/')}/{lyr['id']}"
            name = lyr.get("name", "Layer")
            child = QTreeWidgetItem([name, lyr.get("geometryType", ""), "", ""])
            child.setData(0, DATA_ROLE,
                          {"type": "layer", "url": layer_url,
                           "name": name, "kind": kind})
            node.addChild(child)

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, DATA_ROLE)
        if not data or not isinstance(data, dict):
            return

        menu = QMenu(self.tree)
        refresh_icon = QgsApplication.getThemeIcon("/mActionRefresh.svg")

        # Folder node
        if data.get("type") == "folder":
            add_all = QAction("Add all to map", menu)
            add_all.triggered.connect(lambda: self._add_folder_to_map(data))
            menu.addAction(add_all)
            menu.addSeparator()
            ra = QAction(refresh_icon, "Refresh", menu)
            ra.triggered.connect(lambda: self._refresh_node(item, data))
            menu.addAction(ra)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return

        # Connection node — refresh + re-auth
        if data.get("type") == "connection":
            ra = QAction(refresh_icon, "Refresh", menu)
            ra.triggered.connect(lambda: self._refresh_tree())
            menu.addAction(ra)
            rauth = QAction(refresh_icon, "Refresh and re-authenticate…", menu)
            rauth.triggered.connect(lambda: self._reauth_connection(data))
            menu.addAction(rauth)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return

        client = _CS.instance().any_client()
        if not client:
            return

        menu = QMenu(self.tree)

        def _add():
            self.tree.setCurrentItem(item)
            self._add_layer()

        def _save():
            self.tree.setCurrentItem(item)
            self._save_as_dialog(data)

        is_feature = (data.get("kind") == "feature" or
                      data.get("type") == "feature")
        item_meta = data.get("item", {})
        item_id   = item_meta.get("id", "") or data.get("item_id", "")
        is_owner  = (item_meta.get("owner", "") == getattr(client, "username", ""))

        can_delete = (is_owner and bool(item_id) and
                       data.get("type") not in ("layer",))
        build_agol_menu(
            data, menu,
            client        = client,
            on_add_to_map = _add,
            on_add_all    = (_add if data.get("type") not in ("layer",) else None),
            on_save_as    = _save if is_feature else None,
            on_delete     = (lambda _d=data: self._delete_dsm_item(_d))
                             if can_delete else None,
            parent_widget = self,
        )

        # Always add refresh at bottom
        if menu.actions():
            menu.addSeparator()
        ra = QAction(refresh_icon, "Refresh", menu)
        ra.triggered.connect(lambda: self._refresh_node(item, data))
        menu.addAction(ra)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _refresh_node(self, item, data):
        """Collapse and re-expand the node to reload its children."""
        self.tree.collapseItem(item)
        item.takeChildren()
        ph = QTreeWidgetItem(["Loading…", "", "", ""])
        ph.setData(0, DATA_ROLE, "__placeholder__")
        item.addChild(ph)
        self.tree.expandItem(item)

    def _reauth_connection(self, data):
        """Sign out and force re-authentication."""
        name = self._current_name()
        if name:
            _CS.instance().sign_out(name)
            client = _CS.instance().ensure_client(name, parent=self)
            if client:
                self._load_connection(name)

    def _add_folder_to_map(self, data: dict):
        """Load all services in a DSM folder onto the map."""
        from ..agol_client import SERVICE_TYPES
        from ..dialogs.browser_dialog import _load_layer_from_item
        client = _CS.instance().any_client()
        if not client:
            return
        folder_id = data.get("folder_id", "")
        try:
            services = client.get_folder_items(folder_id)
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(self, "AGOL", str(e))
            return
        for svc in services:
            kind = SERVICE_TYPES.get(svc.get("type", ""), "feature")
            url  = svc.get("url", "")
            if not url:
                continue
            try:
                layers = client.get_service_layers(url)
                for lyr in layers:
                    lurl = f"{url.rstrip('/')}/{lyr['id']}"
                    _load_layer_from_item(client, lurl, kind, lyr.get("name","Layer"))
            except Exception:
                pass

    def _delete_dsm_item(self, data: dict):
        """Delete an item from AGOL after confirmation."""
        from qgis.PyQt.QtWidgets import QMessageBox
        item_meta = data.get("item", {})
        name    = item_meta.get("title", data.get("name", "this item"))
        item_id = item_meta.get("id", "")
        if not item_id:
            return
        client = _CS.instance().any_client()
        if not client:
            return
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
            # Refresh the current connection view
            name_conn = self._current_name()
            if name_conn:
                self._load_connection(name_conn)
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))

    def _save_as_dialog(self, data: dict):
        """Fetch GeoJSON and open QGIS Save As dialog."""
        import json, tempfile, os
        from qgis.core import QgsVectorLayer
        from qgis.gui import QgsVectorLayerSaveAsDialog
        from qgis.PyQt.QtWidgets import QMessageBox
        client = _CS.instance().any_client()
        url = data.get("url", "")
        if not url or not client:
            return
        try:
            from ..dialogs.settings_dialog import SettingsDialog as _S
            geojson = client.query_layer(url, max_record_count=_S.max_features())
        except Exception as e:
            QMessageBox.critical(self, "AGOL", str(e))
            return
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp = f.name
        layer = QgsVectorLayer(tmp, data.get("name", "layer"), "ogr")
        if not layer.isValid():
            try: os.unlink(tmp)
            except Exception: pass
            return
        dlg = QgsVectorLayerSaveAsDialog(layer, parent=self)
        if dlg.exec():
            from qgis.core import QgsVectorFileWriter
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, dlg.filename(), layer.transformContext(), dlg.options()
            )
            if err != QgsVectorFileWriter.WriterError.NoError:
                QMessageBox.critical(self, "Save failed", msg)
        del layer
        try:
            os.unlink(tmp)
        except Exception:
            pass

    def _on_selection_changed(self, current: QTreeWidgetItem, previous):
        """Enable Add button and populate url/CRS labels when a row is selected."""
        if not current:
            self.add_btn.setEnabled(False)
            self._url_label.setText("")
            self._crs_label.setText("")
            self._add_filter_btn.setEnabled(False)
            return
        data = current.data(0, DATA_ROLE)
        if not isinstance(data, dict):
            self.add_btn.setEnabled(False)
            self._url_label.setText("")
            self._crs_label.setText("")
            return
        is_layer = (data.get("type") == "layer")
        is_feature_layer = is_layer and data.get("kind") == "feature"
        self.add_btn.setEnabled(is_layer)
        self._add_filter_btn.setEnabled(is_feature_layer)

        # Web service link — prefer layer URL, fall back to service URL
        url = (data.get("url")
               or data.get("item", {}).get("url", ""))
        if url:
            self._url_label.setText(
                f'<a href="{url}">{url}</a>'
            )
        else:
            self._url_label.setText("")

        # CRS — from item spatial reference if available, else async fetch
        item_meta = data.get("item", {})
        sr = item_meta.get("spatialReference", {})
        if not isinstance(sr, dict):
            sr = {}   # some AGOL responses return a string wkid directly
        wkid = sr.get("latestWkid") or sr.get("wkid", "")
        if wkid:
            self._set_crs(int(wkid))
        elif url and is_layer:
            # Fetch CRS async from service JSON
            self._fetch_crs(url)
        else:
            self._crs_label.setText("")

    def _set_crs(self, wkid: int):
        try:
            from qgis.core import QgsCoordinateReferenceSystem
            crs = QgsCoordinateReferenceSystem(f"EPSG:{wkid}")
            if crs.isValid():
                self._crs_label.setText(
                    f"EPSG:{wkid} - {crs.description()}"
                )
                return
        except Exception:
            pass
        self._crs_label.setText(f"EPSG:{wkid}")

    def _fetch_crs(self, layer_url: str):
        """Async fetch of service JSON to extract CRS wkid."""
        self._crs_label.setText("Loading…")
        client = _CS.instance().any_client()
        if not client:
            self._crs_label.setText("")
            return
        import re as _re
        svc_url = _re.sub(r"/[0-9]+$", "", layer_url.rstrip("/"))
        w = Worker(client._get, svc_url, {"f": "json"})

        def _on_result(detail):
            sr = (detail.get("extent") or
                  detail.get("fullExtent") or {}).get("spatialReference", {})
            if not sr:
                sr = detail.get("spatialReference", {})
            wkid = sr.get("latestWkid") or sr.get("wkid", "")
            if wkid:
                self._set_crs(int(wkid))
            else:
                self._crs_label.setText("")

        w.result.connect(_on_result)
        w.error.connect(lambda _: self._crs_label.setText(""))
        w.finished.connect(w.deleteLater)
        w.start()
        self._crs_worker = w

    def _on_item_double_clicked(self, item: QTreeWidgetItem, col: int):
        data = item.data(0, DATA_ROLE)
        if data and isinstance(data, dict) and data.get("type") == "layer":
            self.add_btn.setEnabled(True)
            self._add_layer()

    def _refresh_tree(self):
        """Refresh the folder/service tree for the active connection."""
        name = self._current_name()
        if name and _CS.instance().is_signed_in(name):
            self._load_connection(name)

    def _on_search(self, text: str):
        """Filter tree items to those whose name contains the search text."""
        text = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            self._filter_item(self.tree.topLevelItem(i), text)

    def _filter_item(self, item, text: str) -> bool:
        match = (not text) or (text in item.text(0).lower())
        child_vis = False
        for i in range(item.childCount()):
            if self._filter_item(item.child(i), text):
                child_vis = True
        visible = match or child_vis
        item.setHidden(not visible)
        if child_vis:
            item.setExpanded(True)
        return visible

    def _load_connections_file(self):
        from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox
        fname, _ = QFileDialog.getOpenFileName(
            self, "Load connections", "", "XML files (*.xml)"
        )
        if fname:
            QMessageBox.information(
                self, "Not yet implemented",
                "Importing connections from XML is not yet available.\n"
                "Use AGOL Connector menu → Connections… to add connections."
            )

    def _save_connections_file(self):
        from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save connections", "agol_connections.xml", "XML files (*.xml)"
        )
        if fname:
            QMessageBox.information(
                self, "Not yet implemented",
                "Exporting connections to XML is not yet available."
            )

    def _add_layer_with_filter(self):
        """Add with a WHERE clause filter pre-applied."""
        sel = self.tree.currentItem()
        if not sel:
            return
        data = sel.data(0, DATA_ROLE)
        if not data or data.get("type") != "layer" or data.get("kind") != "feature":
            self._add_layer()
            return
        from qgis.PyQt.QtWidgets import QInputDialog
        from qgis.PyQt.QtWidgets import QInputDialog
        prompt = ("Enter a WHERE clause to filter features:\n"
                  "(e.g.  STATE_NAME = 'California'  or  POP > 1000000)")
        where, ok = QInputDialog.getText(
            self, "Add with filter", prompt, text="1=1",
        )
        if not ok:
            return
        client = self._active_client
        if not client:
            return
        from ..dialogs.settings_dialog import SettingsDialog as _S
        from ..dialogs.browser_dialog import _load_layer_from_item
        import json, tempfile
        try:
            geojson = client.query_layer(
                data["url"], where=where,
                max_record_count=_S.max_features(),
            )
        except Exception as e:
            QMessageBox.critical(self, "Query failed", str(e))
            return
        import tempfile, json, os as _os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp = f.name
        from qgis.core import QgsVectorLayer, QgsProject
        name = data.get("name", "layer") + f" [{where}]"
        layer = QgsVectorLayer(tmp, name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            QMessageBox.critical(self, "AGOL", f"Could not load filtered layer.")
        del layer
        try: _os.unlink(tmp)
        except Exception: pass

    def _close_panel(self):
        """Close the Data Source Manager."""
        parent = self.parent()
        while parent:
            # Walk up to find the DSM dialog and close it
            if hasattr(parent, "reject"):
                try:
                    parent.reject()
                    return
                except Exception:
                    pass
            parent = parent.parent() if hasattr(parent, "parent") else None

    def _open_help(self):
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(
            "https://github.com/MutantKiwi/agol-connector-qgis"
        ))

    def _add_layer(self):
        sel = self.tree.currentItem()
        if not sel:
            return
        data = sel.data(0, DATA_ROLE)
        if not data or not isinstance(data, dict):
            return
        if data.get("type") == "layer":
            client = self._active_client
            if client:
                from ..dialogs.browser_dialog import _load_layer_from_item
                _load_layer_from_item(
                    client, data["url"], data["kind"], data["name"]
                )
                # addLayer signal signature differs between QGIS 3 and 4.
                # The layer has already been added to QgsProject directly
                # inside _load_layer_from_item, so no signal emit needed.
        elif data.get("type") == "service":
            self.add_btn.setEnabled(True)

    # ── Worker ─────────────────────────────────────────────────────────

    def _run(self, fn, *args, on_result=None):
        self.progress.setVisible(True)
        self._worker = Worker(fn, *args)
        if on_result:
            self._worker.result.connect(on_result)
        self._worker.result.connect(lambda _: self.progress.setVisible(False))
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self.progress.setVisible(False))
        self._worker.start()

    def _on_error(self, msg: str):
        self.progress.setVisible(False)
        QMessageBox.critical(self, "AGOL error", msg)


# ── Source select provider ─────────────────────────────────────────────────

class AGOLSourceSelectProvider(QgsSourceSelectProvider):

    def providerKey(self) -> str:
        return "AGOL"

    def text(self) -> str:
        return "ArcGIS Online"

    def toolTip(self) -> str:
        return "Connect to ArcGIS Online feature, map and image services"

    def icon(self) -> QIcon:
        import os as _os
        p = _os.path.join(_os.path.dirname(__file__), "..", "resources", "icon_agol_root.png")
        return QIcon(p) if _os.path.exists(p) else QgsApplication.getThemeIcon("/mIconConnect.svg")

    def ordering(self) -> int:
        return 0

    def createDataSourceWidget(self, parent=None, fl=Qt.WindowType.Widget,
                                widgetMode=0):
        return AGOLSourceSelectWidget(parent, fl, widgetMode)


import os
