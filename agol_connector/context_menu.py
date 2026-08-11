"""
context_menu.py — Shared AGOL context menu builder
===================================================
Used by all three panels: QGIS Browser, dock panel, Data Source Manager.

Data dict schema:
  {
    "type":         "folder"|"service"|"layer"|"feature"|"webmap",
    "kind":         "feature"|"map"|"image"|"tile"|"webmap",
    "service_kind": same as kind (alternate key),
    "url":          layer or service REST URL,
    "name":         display name,
    "item":         full AGOL item dict (id, title, owner, type, url, …),
    "item_id":      shortcut for item["id"],
    "folder_id":    folder ID (for folder nodes),
  }
"""

from __future__ import annotations
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox


def build_agol_menu(data: dict, menu: QMenu,
                    client,
                    on_add_to_map=None,
                    on_add_all=None,
                    on_save_as=None,
                    on_delete=None,
                    parent_widget=None) -> None:
    """
    Populate `menu` with context actions appropriate for `data`.
    All callbacks are optional — pass None to omit that action.
    """
    kind      = data.get("type", "")
    svc_kind  = data.get("kind") or data.get("service_kind", "")
    item_meta = data.get("item", {})
    layer_url = data.get("url", "")
    name      = data.get("name", item_meta.get("title", "Layer"))

    if kind == "folder":
        # Folder actions: add all + open in browser
        if on_add_all:
            act = QAction("Add all to map", menu)
            act.triggered.connect(on_add_all)
            menu.addAction(act)
        folder_url = _portal_url(client, item_meta)
        if folder_url:
            menu.addSeparator()
            _open_in_browser_action(menu, folder_url)
        return

    # ── Service / layer ────────────────────────────────────────────────

    # Add to map (single layer or first layer of service)
    if on_add_to_map:
        label = "Add to map"
        if kind in ("webmap",):
            label = "Add all layers to map"
        act = QAction(label, menu)
        act.triggered.connect(on_add_to_map)
        menu.addAction(act)

    # Add all layers (multi-layer services)
    if on_add_all and kind not in ("layer", "webmap"):
        act = QAction("Add all layers to map", menu)
        act.triggered.connect(on_add_all)
        menu.addAction(act)

    # Save As (vector/feature layers only)
    is_feature = (
        svc_kind == "feature" or
        kind in ("feature",) or
        (kind == "layer" and data.get("service_kind") == "feature")
    )
    if on_save_as and is_feature:
        act = QAction("Save As…", menu)
        act.triggered.connect(on_save_as)
        menu.addAction(act)

    menu.addSeparator()

    # Open service URL in browser
    service_url = item_meta.get("url") or layer_url
    if service_url:
        _open_in_browser_action(menu, service_url, "Open service URL in browser")

    # Open item page on AGOL portal
    item_id = item_meta.get("id") or data.get("item_id", "")
    if item_id and client:
        portal = getattr(client, "portal_url", "https://www.arcgis.com")
        item_page = f"{portal}/home/item.html?id={item_id}"
        _open_in_browser_action(menu, item_page, "View item on ArcGIS Online")

    # Properties
    if item_meta or layer_url:
        menu.addSeparator()
        act = QAction("Properties…", menu)
        act.triggered.connect(
            lambda: _open_properties(item_meta, layer_url, svc_kind, client, parent_widget)
        )
        menu.addAction(act)

    # Delete — only show for items the current user owns
    if client and item_id:
        is_owner = (item_meta.get("owner", "") == getattr(client, "username", None))
        if is_owner or on_delete:
            menu.addSeparator()
            del_act = QAction("Delete…", menu)
            del_act.triggered.connect(
                on_delete if on_delete else
                lambda: _delete_item(item_id, name, client, parent_widget)
            )
            menu.addAction(del_act)


def _open_in_browser_action(menu: QMenu, url: str, label: str = "Open in browser") -> None:
    act = QAction(label, menu)
    act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
    menu.addAction(act)


def _portal_url(client, item_meta: dict) -> str:
    if not client:
        return ""
    portal = getattr(client, "portal_url", "https://www.arcgis.com")
    item_id = item_meta.get("id", "")
    if item_id:
        return f"{portal}/home/item.html?id={item_id}"
    return ""


def _open_properties(item_meta: dict, layer_url: str,
                     service_kind: str, client, parent=None) -> None:
    from .dialogs.agol_properties_dialog import AGOLPropertiesDialog
    AGOLPropertiesDialog(
        item_meta=item_meta, layer_url=layer_url,
        service_kind=service_kind, client=client, parent=parent,
    ).exec()


def _delete_item(item_id: str, name: str, client, parent=None) -> None:
    """Standalone delete with confirmation — used when no on_delete callback given."""
    msg = f"Permanently delete '{name}' from ArcGIS Online?\n\nThis cannot be undone."
    r = QMessageBox.warning(
        parent, "Delete item", msg,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if r != QMessageBox.StandardButton.Yes:
        return
    try:
        client.delete_item(item_id)
    except Exception as e:
        QMessageBox.critical(parent, "Delete failed", str(e))
