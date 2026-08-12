"""
compat.py  —  QGIS 3 / QGIS 4 enum compatibility
==================================================
Single source of truth for all version-specific enum lookups.
Import constants from here rather than using version-specific paths directly.
"""
from qgis.core import QgsDataItem, QgsDataItemProvider, QgsLayerItem, Qgis


def _first(*fns):
    """Return the value of the first callable that doesn't raise AttributeError."""
    for fn in fns:
        try:
            v = fn()
            if v is not None:
                return v
        except AttributeError:
            pass
    raise AttributeError("None of the candidates resolved")


# ── QgsDataItem ────────────────────────────────────────────────────────────
STATE_POPULATED = _first(
    lambda: QgsDataItem.State.Populated,    # QGIS 4
    lambda: QgsDataItem.Populated,          # QGIS 3
)
TYPE_MESSAGE = _first(
    lambda: QgsDataItem.Type.Message,       # QGIS 4
    lambda: QgsDataItem.Custom,             # QGIS 3
)

# ── QgsLayerItem ───────────────────────────────────────────────────────────
LT_VECTOR = _first(
    lambda: QgsLayerItem.LayerType.Vector,  # QGIS 4
    lambda: QgsLayerItem.Vector,            # QGIS 3
)
LT_RASTER = _first(
    lambda: QgsLayerItem.LayerType.Raster,  # QGIS 4
    lambda: QgsLayerItem.Raster,            # QGIS 3
)

# ── Qgis.MessageLevel ─────────────────────────────────────────────────────
MSG_SUCCESS  = _first(lambda: Qgis.MessageLevel.Success,  lambda: Qgis.Success)
MSG_WARNING  = _first(lambda: Qgis.MessageLevel.Warning,  lambda: Qgis.Warning)
MSG_INFO     = _first(lambda: Qgis.MessageLevel.Info,     lambda: Qgis.Info)
MSG_CRITICAL = _first(lambda: Qgis.MessageLevel.Critical, lambda: Qgis.Critical)

# ── QgsDataItemProvider capabilities ──────────────────────────────────────
# IMPORTANT: returning NoCapabilities(0) is correct for a root-item-only
# provider. The QGIS browser model calls createDataItem("", None) for ALL
# registered providers regardless of capability value.
# However, in some QGIS 3 builds the model uses capability flags to decide
# whether to include the provider at all. Use "Other" (4) to be safe.
def provider_capabilities():
    """
    Return the capability flags value for QgsDataItemProvider.capabilities().

    QGIS 4.x requires a proper QFlags object — a plain int raises TypeError.
    We try multiple enum paths in order from newest to oldest QGIS API.
    """
    # QGIS 4.x: QgsDataItemProvider.Capability is a proper enum
    # capabilities() must return QgsDataItemProvider.Capabilities (QFlags wrapper)
    try:
        from qgis.core import QgsDataItemProvider as _P
        cap = _P.Capability.Other           # enum member
        try:
            return _P.Capabilities(cap)     # QGIS 4: wrap in QFlags
        except Exception:
            return cap                      # already the right type
    except AttributeError:
        pass

    # QGIS 3.x nested enum
    try:
        return QgsDataItemProvider.Capability.Other
    except AttributeError:
        pass

    # QGIS 3.x flat enum
    try:
        return QgsDataItemProvider.Other
    except AttributeError:
        pass

    # Last resort: ask a probe instance for its own capabilities type
    class _Probe(QgsDataItemProvider):
        def name(self): return "_probe_"
        def capabilities(self): return super().capabilities()
        def createDataItem(self, p, par): return None
    base = _Probe().capabilities()
    try:
        return base | 4
    except TypeError:
        return base
