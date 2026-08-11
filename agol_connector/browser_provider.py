"""
browser_provider.py  —  QGIS 3 & 4 compatible
==============================================
AGOLBrowserNodeGuiProvider — context menus for AGOL browser tree nodes.

The old AGOLFileExportGuiProvider (adding "Export to ArcGIS Online…"
to vector files) is removed — export is now available via the Layers
panel right-click and the AGOL Connector menu.
"""

from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox
from qgis.gui import QgsDataItemGuiProvider
from qgis.core import QgsDataItem

from .context_menu import build_agol_menu
from .credentials import CredentialStore
from .provider.agol_data_items import (
    AGOLRootItem, AGOLConnectionItem, AGOLFolderItem,
    AGOLServiceItem, AGOLLayerItem, AGOLBrowserGuiProvider,
)


class AGOLBrowserNodeGuiProvider(QgsDataItemGuiProvider):
    """
    Handles right-click menus on all AGOL browser tree nodes.
    Uses the shared build_agol_menu() so behaviour matches the
    dock panel and Data Source Manager exactly.
    """

    def __init__(self, iface):
        super().__init__()
        self._iface   = iface
        self._handler = AGOLBrowserGuiProvider(iface)

    def name(self) -> str:
        return "AGOLBrowserNodes"

    def populateContextMenu(self, item: QgsDataItem, menu: QMenu,
                             selected_items, context) -> None:

        # Connection management items (root, connection)
        if isinstance(item, (AGOLRootItem, AGOLConnectionItem,
                              AGOLFolderItem)):
            self._handler.populate_menu(item, menu)
            return

        # Service or layer — use shared menu builder
        parent_w = self._iface.mainWindow() if self._iface else None

        if isinstance(item, AGOLServiceItem):
            svc  = item._meta
            kind = item._kind
            data = {"type": kind or "service", "item": svc, "kind": kind,
                    "url": svc.get("url", ""), "name": svc.get("title", ""),
                    "item_id": svc.get("id", "")}
            is_owner = (svc.get("owner","") ==
                        getattr(item._client, "username", ""))
            build_agol_menu(
                data, menu,
                client        = item._client,
                on_add_to_map = item._add_first_layer,
                on_add_all    = item._add_all_layers,
                on_delete     = (lambda: self._delete_service(item)
                                 if is_owner else None),
                parent_widget = parent_w,
            )
            return

        if isinstance(item, AGOLLayerItem):
            data = {
                "type":         "layer",
                "kind":         item.service_kind,
                "service_kind": item.service_kind,
                "url":          item.layer_url,
                "name":         item.name(),
                "item":         {},
            }
            build_agol_menu(
                data, menu,
                client        = item.client,
                on_add_to_map = item._load,
                on_save_as    = lambda: self._save_as(item),
                parent_widget = parent_w,
            )

    def _delete_service(self, item: AGOLServiceItem):
        """Delete a service item after confirmation, then refresh parent."""
        from .context_menu import _delete_item
        parent = self._iface.mainWindow() if self._iface else None
        item_id = item._meta.get("id", "")
        name    = item.name()
        if not item_id:
            return
        from qgis.PyQt.QtWidgets import QMessageBox
        msg = "Permanently delete '" + name + "' from ArcGIS Online?\n\nThis cannot be undone."
        r = QMessageBox.warning(
            parent, "Delete item", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            item._client.delete_item(item_id)
            if item.parent():
                item.parent().refresh()
        except Exception as e:
            QMessageBox.critical(parent, "Delete failed", str(e))

    def _save_as(self, item: AGOLLayerItem):
        """Fetch GeoJSON then open QGIS Save As dialog."""
        from .dialogs.browser_dialog import _load_layer_from_item
        import json, tempfile, os
        from qgis.core import QgsVectorLayer
        from qgis.gui import QgsVectorLayerSaveAsDialog
        client = item.client
        try:
            from .dialogs.settings_dialog import SettingsDialog as _S
            geojson = client.query_layer(item.layer_url, max_record_count=_S.max_features())
        except Exception as e:
            QMessageBox.critical(None, "AGOL", str(e))
            return
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        ) as f:
            json.dump(geojson, f)
            tmp = f.name
        layer = QgsVectorLayer(tmp, item.name(), "ogr")
        if not layer.isValid():
            try: os.unlink(tmp)
            except Exception: pass
            return
        parent = self._iface.mainWindow() if self._iface else None
        dlg = QgsVectorLayerSaveAsDialog(layer, parent=parent)
        if dlg.exec():
            from qgis.core import QgsVectorFileWriter
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, dlg.filename(), layer.transformContext(), dlg.options()
            )
            if err != QgsVectorFileWriter.WriterError.NoError:
                QMessageBox.critical(parent, "Save failed", msg)
        # Release layer before deleting temp file (required on Windows)
        del layer
        try: os.unlink(tmp)
        except Exception: pass
