"""
layer_action.py  —  QGIS 3 & 4 compatible
==========================================
Adds "Save to ArcGIS Online…" to the Layers panel right-click menu
via QgsMapLayerAction signal connection (not subclassing).

QgsMapLayerAction constructor signature differs between versions:
  QGIS 3: QgsMapLayerAction(name, parent, targets, flags)
  QGIS 4: similar but enum paths differ

We use the signal 'triggeredForLayer' which exists in both versions.
"""

from qgis.PyQt.QtCore import QObject
from qgis.gui import QgsMapLayerAction
from qgis.core import QgsMapLayer

from .credentials import CredentialStore


def _make_layer_action(iface) -> QgsMapLayerAction:
    """
    Create a QgsMapLayerAction that opens the AGOL upload dialog.
    Returns the action (caller must keep a reference and register it).
    """
    # Try to construct with targets=AllActions (shows for all layer types)
    # Use a QObject as parent so Qt manages lifetime
    _parent = iface.mainWindow()

    action = None

    # Try QGIS 3/4 style with explicit targets
    for targets_val in [
        lambda: QgsMapLayerAction.LayerType.AllLayers,       # QGIS 4 nested
        lambda: QgsMapLayerAction.AllLayers,                 # QGIS 3/4 flat
    ]:
        try:
            action = QgsMapLayerAction(
                "Save to ArcGIS Online…",
                _parent,
                targets_val(),
            )
            break
        except (AttributeError, TypeError):
            pass

    if action is None:
        # Fallback: no targets argument (defaults to AllActions in most builds)
        try:
            action = QgsMapLayerAction("Save to ArcGIS Online…", _parent)
        except Exception:
            action = QgsMapLayerAction("Save to ArcGIS Online…", None)

    # Connect the triggered signal — try both QGIS 3 and 4 signal names
    def _on_trigger(layer, *args):
        _upload(layer, iface)

    for sig_name in ("triggeredForLayer", "triggered"):
        sig = getattr(action, sig_name, None)
        if sig is not None:
            try:
                sig.connect(_on_trigger)
                break
            except TypeError:
                pass

    return action


def _upload(layer: QgsMapLayer, iface):
    """Open the upload dialog, signing in first if needed."""
    store = CredentialStore.instance()
    client = store.any_client()
    if not client:
        names = store.connection_names()
        client = store.ensure_client(
            names[0] if names else "",
            parent=iface.mainWindow() if iface else None,
        )
    if not client:
        return
    from .dialogs.upload_dialog import UploadDialog
    dlg = UploadDialog(client, layer, iface.mainWindow())
    dlg.exec()
