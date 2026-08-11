"""
agol_connector.py — QGIS 3 & 4 compatible main plugin
- Toolbar: status pill only
- Plugin menu "AGOL Connector" → Browse, Upload, Connections, Settings, About
- Auto-load saved credentials on startup
"""

import os
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QToolBar, QMenu, QMessageBox, QDialog,
    QVBoxLayout, QLabel, QDialogButtonBox,
)
from qgis.core import QgsApplication
from qgis.gui import QgsGui

from .compat import MSG_SUCCESS, MSG_WARNING
from .credentials import CredentialStore
from .dialogs.browser_dialog import BrowserPanel
from .dialogs.upload_dialog import UploadDialog
from .dialogs.connection_dialog import ConnectionDialog
from .browser_provider import AGOLBrowserNodeGuiProvider
from .provider.agol_data_items import AGOLDataItemProvider
from .provider.agol_source_select import AGOLSourceSelectProvider
from .layer_action import _make_layer_action

_PLUGIN_VERSION = "0.2.0"


class AGOLConnector:

    def __init__(self, iface):
        self.iface      = iface
        self.plugin_dir = os.path.dirname(__file__)
        self._store     = CredentialStore.instance()
        self.toolbar: QToolBar | None = None
        self.actions: list = []

        self._browser_panel      = None
        self._node_gui_prov      = None
        self._data_item_prov     = None
        self._source_select_prov = None
        self._layer_action       = None
        self._status_action: QAction | None = None

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def initGui(self):
        QTimer.singleShot(200, self._auto_load)
        # Initialise error tracking (Bugsink via Sentry SDK)
        try:
            from .error_tracking import init_error_tracking
            init_error_tracking()
        except Exception:
            pass

        try:
            from .progress_manager import ProgressManager
            ProgressManager.instance().set_iface(self.iface)
        except Exception:
            pass


        # ── Toolbar: status pill only ─────────────────────────────────
        self.toolbar = self.iface.addToolBar("AGOL Connector")
        self.toolbar.setObjectName("AGOLConnectorToolbar")

        self._status_action = QAction(
            QIcon(os.path.join(self.plugin_dir, "resources", "icon_agol_root.png")),
            "AGOL: not signed in",
            self.iface.mainWindow(),
        )
        self._status_action.setToolTip("AGOL Connector — click to sign in or manage")
        self._status_action.triggered.connect(self._on_status_click)
        self.toolbar.addAction(self._status_action)

        # ── Top-level menu bar entry "AGOL Connector" ─────────────────
        # Build a QMenu and insert it into the menu bar before Help.
        self._agol_menu = QMenu("AGOL Connector", self.iface.mainWindow())
        self._agol_menu.setObjectName("AGOLConnectorMenu")

        self._agol_menu.addAction(self._make_action(
            "icon_browse.png", "Browse services",
            self.toggle_browser_panel,
            "Browse and load AGOL services",
        ))
        self._agol_menu.addAction(self._make_action(
            "icon_upload.png", "Upload layer to AGOL",
            self.open_upload_dialog,
            "Upload the active layer to ArcGIS Online",
        ))
        self._agol_menu.addSeparator()
        self._agol_menu.addAction(self._make_action(
            "icon_connect.png", "Connections…",
            self._manage_connections,
            "Add, edit and remove AGOL connections",
        ))
        self._agol_menu.addAction(self._make_action(
            "icon_settings.png", "Settings…",
            self._open_settings,
            "Plugin settings",
        ))
        self._agol_menu.addSeparator()
        self._agol_menu.addSeparator()
        self._agol_menu.addAction(self._make_action(
            "icon_agol_root.png", "About…",
            self._open_about,
            "About AGOL Connector",
        ))

        # Insert directly into the menu bar before Help (or at the end).
        # QGIS localises menu names so we match on both English text and
        # position — the Help menu is always the last top-level menu.
        menu_bar = self.iface.mainWindow().menuBar()
        all_actions = [a for a in menu_bar.actions() if a.menu()]
        insert_before = None
        for action in all_actions:
            clean = action.text().lower().replace("&", "").strip()
            if clean in ("help", "aide", "ayuda", "hilfe", "aiuto"):
                insert_before = action
                break
        if insert_before is None and all_actions:
            # Fallback: insert before the last top-level menu
            insert_before = all_actions[-1]

        if insert_before:
            menu_bar.insertMenu(insert_before, self._agol_menu)
        else:
            menu_bar.addMenu(self._agol_menu)

        # ── Providers ─────────────────────────────────────────────────
        self._node_gui_prov = AGOLBrowserNodeGuiProvider(self.iface)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self._node_gui_prov)

        self._data_item_prov = AGOLDataItemProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self._data_item_prov)
        QTimer.singleShot(600, self._refresh_browser)

        self._source_select_prov = AGOLSourceSelectProvider()
        QgsGui.sourceSelectProviderRegistry().addProvider(self._source_select_prov)

        self._layer_action = _make_layer_action(self.iface)
        if self._layer_action:
            QgsGui.mapLayerActionRegistry().addMapLayerAction(self._layer_action)

        # Also add to layer right-click via iface custom action
        # (appears under the layer name in the Layers panel context menu)
        self._layer_export_action = QAction(
            QIcon(os.path.join(self.plugin_dir, "resources", "icon_upload.png")),
            "Save to AGOL…",
            self.iface.mainWindow(),
        )
        self._layer_export_action.triggered.connect(self._export_active_layer)
        # Hook into the Layers panel context menu via contextMenuAboutToShow
        # This injects "Save to AGOL…" directly into the existing Export submenu
        try:
            self.iface.layerTreeView().contextMenuAboutToShow.connect(
                self._inject_export_action
            )
        except Exception:
            # Fallback: add as a plain custom action if signal not available
            try:
                from qgis.core import QgsMapLayer
                for layer_type in (QgsMapLayer.VectorLayer, QgsMapLayer.RasterLayer):
                    self.iface.addCustomActionForLayerType(
                        self._layer_export_action, "", layer_type, True
                    )
            except Exception:
                pass

        # ── Processing Provider ───────────────────────────────────────
        try:
            from qgis.core import QgsApplication as _QgsApp
            from .processing.provider import AGOLProcessingProvider
            self._processing_provider = AGOLProcessingProvider()
            _QgsApp.processingRegistry().addProvider(
                self._processing_provider
            )
        except Exception:
            self._processing_provider = None

        # ── Layer > Export > Save to AGOL… ───────────────────────────
        # Find the "Export" submenu inside the Layer menu in the menu bar.
        # This makes "Save to AGOL…" appear alongside "Save As…",
        # "Save Features As…" etc. when the user right-clicks a layer
        # and chooses Export, OR uses the Layer menu → Export.
        try:
            menu_bar = self.iface.mainWindow().menuBar()
            export_menu = None
            for act in menu_bar.actions():
                m = act.menu()
                if not m:
                    continue
                if act.text().replace("&", "").strip().lower() == "layer":
                    for sub_act in m.actions():
                        sub_m = sub_act.menu()
                        if sub_m and "export" in sub_act.text().replace("&", "").lower():
                            export_menu = sub_m
                            break
                    break
            if export_menu is None:
                # No Layer > Export submenu found — skip menu bar entry
                # (still available via right-click Export on layers)
                pass
            else:
                pass
            # Create the action regardless for right-click injection
            self._export_menu_action = QAction(
                QIcon(os.path.join(self.plugin_dir, "resources", "icon_upload.png")),
                "Save to AGOL…",
                self.iface.mainWindow(),
            )
            self._export_menu_action.setToolTip(
                "Upload the active layer to ArcGIS Online"
            )
            self._export_menu_action.triggered.connect(self._export_active_layer)
            if export_menu:
                export_menu.addAction(self._export_menu_action)
        except Exception:
            pass

        # ── Layer > Add Layer > Add ArcGIS Online… ────────────────────
        self._add_agol_action = QAction(
            QIcon(os.path.join(self.plugin_dir, "resources", "icon.png")),
            "Add ArcGIS Online Layer…",
            self.iface.mainWindow(),
        )
        self._add_agol_action.setToolTip(
            "Open the Data Source Manager on the ArcGIS Online tab"
        )
        self._add_agol_action.triggered.connect(self._open_dsm_agol)
        try:
            layer_menu = self.iface.addLayerMenu()
            if layer_menu is not None:
                layer_menu.addAction(self._add_agol_action)
        except Exception:
            pass

    def unload(self):
        if self._layer_action:
            try:
                QgsGui.mapLayerActionRegistry().removeMapLayerAction(self._layer_action)
            except Exception:
                pass
        if hasattr(self, "_layer_export_action") and self._layer_export_action:
            try:
                self.iface.layerTreeView().contextMenuAboutToShow.disconnect(
                    self._inject_export_action
                )
            except Exception:
                pass
            try:
                self.iface.removeCustomActionForLayerType(self._layer_export_action)
            except Exception:
                pass
        if hasattr(self, "_add_agol_action") and self._add_agol_action:
            try:
                layer_menu = self.iface.addLayerMenu()
                if layer_menu is not None:
                    layer_menu.removeAction(self._add_agol_action)
            except Exception:
                pass
        if hasattr(self, "_export_menu_action") and self._export_menu_action:
            try:
                self._export_menu_action.parent().removeAction(
                    self._export_menu_action
                )
            except Exception:
                pass
        if hasattr(self, "_processing_provider") and self._processing_provider:
            try:
                from qgis.core import QgsApplication as _QgsApp
                _QgsApp.processingRegistry().removeProvider(
                    self._processing_provider
                )
            except Exception:
                pass

        for prov, reg in [
            (self._node_gui_prov,      QgsGui.dataItemGuiProviderRegistry()),
            (self._source_select_prov, QgsGui.sourceSelectProviderRegistry()),
        ]:
            if prov:
                try:
                    reg.removeProvider(prov)
                except Exception:
                    pass

        if self._data_item_prov:
            try:
                QgsApplication.dataItemProviderRegistry().removeProvider(
                    self._data_item_prov
                )
            except Exception:
                pass

        if self._browser_panel:
            self.iface.mainWindow().removeDockWidget(self._browser_panel)
            self._browser_panel.deleteLater()
            self._browser_panel = None

        # Remove top-level menu bar entry
        if hasattr(self, "_agol_menu") and self._agol_menu:
            self.iface.mainWindow().menuBar().removeAction(
                self._agol_menu.menuAction()
            )
            self._agol_menu = None

        for action in self.actions:
            self.iface.removeToolBarIcon(action)
        if self.toolbar:
            del self.toolbar

    # ------------------------------------------------------------------ #
    #  Auto-load & status                                                  #
    # ------------------------------------------------------------------ #

    def _auto_load(self):
        """Silently re-authenticate saved connections (respects Settings preference)."""
        from qgis.core import QgsSettings
        if QgsSettings().value("AGOL/settings/auto_load", True, type=bool):
            self._store.auto_load()
        self._update_status()


    def _update_status(self):
        if not self._status_action:
            return
        client = self._store.any_client()
        if client and client.username:
            self._status_action.setText(f"AGOL: {client.username}")
            self._status_action.setToolTip(
                f"Signed in as {client.username} — click to manage"
            )
        else:
            self._status_action.setText("AGOL: sign in…")
            self._status_action.setToolTip(
                "Not signed in — click to sign in to ArcGIS Online"
            )

    # ------------------------------------------------------------------ #
    #  Status pill click                                                   #
    # ------------------------------------------------------------------ #

    def _on_status_click(self):
        client = self._store.any_client()
        if client:
            menu = QMenu(self.iface.mainWindow())
            for name in self._store.connection_names():
                c = self._store.get_client(name)
                if c and c.token:
                    act = menu.addAction(f"● {c.username}  ({name})")
                    act.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Manage connections…").triggered.connect(
                self._manage_connections
            )
            menu.addAction("Sign out all").triggered.connect(self._sign_out_all)
            btn_widget = self.toolbar.widgetForAction(self._status_action)
            if btn_widget:
                menu.exec(btn_widget.mapToGlobal(btn_widget.rect().bottomLeft()))
            else:
                menu.exec(self.iface.mainWindow().cursor().pos())
        else:
            self._sign_in()

    def _sign_in(self):
        names = self._store.connection_names()
        if not names:
            dlg = ConnectionDialog(parent=self.iface.mainWindow())
            if dlg.exec():
                self._store.save_connection(
                    dlg.result_name, dlg.result_url,
                    username=dlg.entered_username if dlg.save_credentials else "",
                    password=dlg.entered_password if dlg.save_credentials else "",
                )
                if dlg.result_client:
                    self._store.set_client(dlg.result_name, dlg.result_client)
                names = [dlg.result_name]
            else:
                return

        for name in names:
            if not self._store.is_signed_in(name):
                client = self._store.ensure_client(
                    name, parent=self.iface.mainWindow()
                )
                if client:
                    self._update_status()
                    self.iface.messageBar().pushMessage(
                        "AGOL", f"Signed in as {client.username}",
                        MSG_SUCCESS, 4,
                    )
                return

    def _sign_out_all(self):
        self._store.sign_out_all()
        self._update_status()
        self.iface.messageBar().pushMessage(
            "AGOL", "Signed out.", MSG_WARNING, 3
        )

    # ------------------------------------------------------------------ #
    #  Menu actions                                                        #
    # ------------------------------------------------------------------ #

    def toggle_browser_panel(self):
        client = self._store.any_client()
        if not client:
            client = self._store.ensure_client(
                self._first_conn(), parent=self.iface.mainWindow()
            )
        if not client:
            return

        if self._browser_panel is None:
            self._browser_panel = BrowserPanel(client, self.iface)
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea,
                self._browser_panel,
            )
            self._browser_panel.show_and_refresh()
        else:
            if self._browser_panel.isVisible():
                self._browser_panel.hide()
            else:
                self._browser_panel.show_and_refresh()

    def open_upload_dialog(self):
        client = self._store.any_client()
        if not client:
            client = self._store.ensure_client(
                self._first_conn(), parent=self.iface.mainWindow()
            )
        if not client:
            return

        layer = self.iface.activeLayer()
        if not layer:
            self.iface.messageBar().pushMessage(
                "AGOL", "No active layer selected.", MSG_WARNING, 4
            )
            return
        dlg = UploadDialog(client, layer, self.iface.mainWindow())
        dlg.exec()

    def _manage_connections(self):
        from .dialogs.manage_connections_dialog import ManageConnectionsDialog
        dlg = ManageConnectionsDialog(parent=self.iface.mainWindow())
        dlg.exec()
        self._update_status()

    def _open_settings(self):
        from .dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(parent=self.iface.mainWindow())
        dlg.exec()

    def _open_about(self):
        from .dialogs.about_dialog import AboutDialog
        AboutDialog(parent=self.iface.mainWindow()).exec()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _export_active_layer(self):
        """Called from layer panel right-click → Export / Save to ArcGIS Online…"""
        layer = self.iface.activeLayer()
        if not layer:
            return
        client = self._store.any_client()
        if not client:
            client = self._store.ensure_client(
                self._first_conn(), parent=self.iface.mainWindow()
            )
        if not client:
            return
        from .dialogs.upload_dialog import UploadDialog
        UploadDialog(client, layer, self.iface.mainWindow()).exec()

    def _inject_export_action(self, menu):
        """
        Called when the Layers panel context menu is about to show.
        Finds the existing 'Export' submenu and injects 'Save to AGOL…' into it.
        """
        from qgis.PyQt.QtWidgets import QMenu
        for action in menu.actions():
            sub = action.menu()
            if sub and action.text().replace("&", "").strip().lower() == "export":
                # Check not already added
                existing = [a.text() for a in sub.actions()]
                if "Save to AGOL…" not in existing:
                    sub.addSeparator()
                    sub.addAction(self._layer_export_action)
                return

    def _open_log(self):
        """Open the AGOL Connector log file in the system text editor."""
        from .logger import log as _log
        opened = _log.open_log_file()
        if not opened:
            from qgis.PyQt.QtWidgets import QMessageBox
            path = _log.log_file_path or "unknown"
            QMessageBox.information(
                self.iface.mainWindow(), "Log",
                f"Log file not found or could not be opened.\n\nExpected location:\n{path}"
            )

    def _open_dsm_agol(self):
        """Open Data Source Manager with the ArcGIS Online tab active."""
        # Strategy 1: trigger QGIS's own "Open Data Source Manager" action
        # then switch to our tab programmatically.
        from qgis.PyQt.QtWidgets import QAction
        opened = False

        # Find and trigger the existing DSM action (most reliable)
        for obj_name in ("mActionDataSourceManager", "mActionAddLayer"):
            action = self.iface.mainWindow().findChild(QAction, obj_name)
            if action:
                action.trigger()
                opened = True
                break

        if not opened:
            # Strategy 2: directly instantiate QgsDataSourceManagerDialog
            try:
                from qgis.gui import QgsDataSourceManagerDialog
                dlg = QgsDataSourceManagerDialog(
                    self.iface.layerTreeView().layerTreeModel(),
                    self.iface.mainWindow(),
                    self.iface.mapCanvas(),
                )
                dlg.setAttribute(
                    Qt.WidgetAttribute.WA_DeleteOnClose
                )
                dlg.show()
                opened = True
            except Exception:
                pass

        if not opened:
            # Final fallback
            try:
                self.iface.openDataSourceManagerDialog()
            except Exception:
                pass

        # Switch to our tab after the dialog is open
        if opened:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(100, self._switch_dsm_to_agol)

    def _switch_dsm_to_agol(self):
        """Find the open DSM dialog and switch it to our ArcGIS Online tab."""
        from qgis.PyQt.QtWidgets import QDialog, QListWidget
        for widget in self.iface.mainWindow().findChildren(QDialog):
            if "data source" not in widget.windowTitle().lower():
                continue
            list_widgets = widget.findChildren(QListWidget)
            for lw in list_widgets:
                best_row = -1
                for i in range(lw.count()):
                    item = lw.item(i)
                    if not item:
                        continue
                    text = item.text().lower()
                    # Match "arcgis online" exactly — not "arcgis rest server"
                    if "online" in text:
                        best_row = i
                        break   # exact match, stop looking
                    if "arcgis" in text and best_row == -1:
                        best_row = i  # fallback, keep looking
                if best_row >= 0:
                    lw.setCurrentRow(best_row)
                    return

    def _first_conn(self) -> str:
        names = self._store.connection_names()
        return names[0] if names else ""

    def _refresh_browser(self):
        try:
            from qgis.gui import QgsBrowserGuiModel
            m = QgsBrowserGuiModel.instance()
            if m:
                m.reload()
                return
        except Exception:
            pass
        try:
            from qgis.gui import QgsBrowserDockWidget
            for dock in self.iface.mainWindow().findChildren(QgsBrowserDockWidget):
                dock.refresh()
        except Exception:
            pass

    def _make_action(self, icon: str | None, text: str,
                     callback, tooltip: str = "") -> QAction:
        """Create an action (used in the top-level menu)."""
        if icon:
            path = os.path.join(self.plugin_dir, "resources", icon)
            action = QAction(QIcon(path), text, self.iface.mainWindow())
        else:
            action = QAction(text, self.iface.mainWindow())
        action.setToolTip(tooltip)
        action.triggered.connect(callback)
        self.actions.append(action)
        return action
