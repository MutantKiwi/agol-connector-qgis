"""
layer_metadata.py — Populate QGIS layer metadata from AGOL item info
=====================================================================
Sets QgsLayerMetadata on any layer loaded from AGOL so the
Layer Properties → Information tab matches the ArcGIS REST Server style.

Call after addMapLayer():
    set_agol_metadata(layer, item_meta, layer_url, service_detail)
"""

from __future__ import annotations
from typing import Optional


def set_agol_metadata(layer,
                      item_meta: dict,
                      layer_url: str = "",
                      service_detail: Optional[dict] = None) -> None:
    """
    Populate layer metadata from AGOL item dict and optional service JSON.

    item_meta   — from AGOL search/content API (title, owner, tags, etc.)
    layer_url   — full REST endpoint URL  (used for Identifier / data URL)
    service_detail — parsed /FeatureServer/0?f=json (fields, extent, CRS, etc.)
    """
    try:
        from qgis.core import (
            QgsLayerMetadata, QgsCoordinateReferenceSystem,
            QgsDateTimeRange, QgsBox3d,
        )
        from datetime import datetime, timezone

        md = QgsLayerMetadata()

        # ── Identification ──────────────────────────────────────────────
        title   = item_meta.get("title", "")
        snippet = item_meta.get("snippet") or item_meta.get("description", "")
        owner   = item_meta.get("owner", "")

        md.setTitle(title)
        md.setAbstract(snippet)
        md.setIdentifier(layer_url or item_meta.get("url", ""))
        md.setParentIdentifier(item_meta.get("url", ""))
        md.setType("dataset")
        md.setLanguage("en")

        # Tags / keywords
        tags = item_meta.get("tags") or item_meta.get("typeKeywords") or []
        if isinstance(tags, list) and tags:
            md.setKeywords({"AGOL": tags})

        # ── Rights / access ─────────────────────────────────────────────
        access_info = item_meta.get("access", "")
        license_info = item_meta.get("licenseInfo", "")
        terms_of_use = item_meta.get("termsOfUse", "")

        constraints = []
        if access_info:
            c = QgsLayerMetadata.Constraint()
            c.type = "access"
            c.constraint = access_info
            constraints.append(c)
        if license_info:
            c = QgsLayerMetadata.Constraint()
            c.type = "license"
            c.constraint = _strip_html(license_info)
            constraints.append(c)
        if terms_of_use:
            c = QgsLayerMetadata.Constraint()
            c.type = "rights"
            c.constraint = _strip_html(terms_of_use)
            constraints.append(c)
        if constraints:
            md.setConstraints(constraints)

        # ── Links ────────────────────────────────────────────────────────
        links = []
        svc_url = layer_url or item_meta.get("url", "")
        if svc_url:
            lnk = QgsLayerMetadata.Link()
            lnk.name = "ArcGIS Online"
            lnk.type = "WWW:LINK"
            lnk.url  = svc_url
            links.append(lnk)
        item_id = item_meta.get("id", "")
        if item_id:
            portal = "https://www.arcgis.com"
            lnk2 = QgsLayerMetadata.Link()
            lnk2.name = "Item page"
            lnk2.type = "WWW:LINK"
            lnk2.url  = f"{portal}/home/item.html?id={item_id}"
            links.append(lnk2)
        if links:
            md.setLinks(links)

        # ── CRS and Extent from service detail ──────────────────────────
        if service_detail:
            _apply_service_detail(md, service_detail, layer)

        layer.setMetadata(md)

        # setDataUrl makes the URL appear in General > URL (clickable)
        if svc_url:
            try:
                layer.setDataUrl(svc_url)
            except Exception:
                pass

    except Exception:
        pass   # metadata is non-critical — never crash the load


def _apply_service_detail(md, detail: dict, layer) -> None:
    """Apply CRS, extent and provider fields from service JSON."""
    from qgis.core import (
        QgsLayerMetadata, QgsCoordinateReferenceSystem,
        QgsBox3d, QgsCoordinateTransform, QgsProject,
    )

    # CRS
    ext = (detail.get("fullExtent") or detail.get("extent") or
           detail.get("initialExtent") or {})
    sr = ext.get("spatialReference", {})
    if not isinstance(sr, dict):
        sr = {}
    wkid = int(sr.get("latestWkid") or sr.get("wkid") or 0)
    if wkid:
        crs = QgsCoordinateReferenceSystem(f"EPSG:{wkid}")
        if crs.isValid():
            md.setCrs(crs)

    # Spatial extent
    try:
        if ext and wkid:
            xmin = float(ext.get("xmin", 0))
            ymin = float(ext.get("ymin", 0))
            xmax = float(ext.get("xmax", 0))
            ymax = float(ext.get("ymax", 0))
            src_crs = QgsCoordinateReferenceSystem(f"EPSG:{wkid}")
            wgs84   = QgsCoordinateReferenceSystem("EPSG:4326")
            if src_crs.isValid() and src_crs != wgs84:
                from qgis.core import QgsRectangle
                xf = QgsCoordinateTransform(
                    src_crs, wgs84, QgsProject.instance()
                )
                rect = xf.transformBoundingBox(
                    __import__("qgis.core", fromlist=["QgsRectangle"])
                    .QgsRectangle(xmin, ymin, xmax, ymax)
                )
                xmin, ymin, xmax, ymax = (
                    rect.xMinimum(), rect.yMinimum(),
                    rect.xMaximum(), rect.yMaximum()
                )
            sp_ext = QgsLayerMetadata.SpatialExtent()
            sp_ext.bounds = QgsBox3d(xmin, ymin, 0, xmax, ymax, 0)
            sp_ext.extentCrs = wgs84
            ex = QgsLayerMetadata.Extent()
            ex.setSpatialExtents([sp_ext])
            md.setExtent(ex)
    except Exception:
        pass


def _strip_html(text: str) -> str:
    """Very basic HTML tag stripper for license/terms fields."""
    import re
    return re.sub(r"<[^>]+>", " ", text).strip()


def apply_metadata_async(layer, item_meta: dict, layer_url: str,
                          client) -> None:
    """
    Apply basic metadata immediately, then fetch service detail async
    and update CRS/extent once it arrives.
    """
    set_agol_metadata(layer, item_meta, layer_url)

    if not layer_url or not client:
        return

    import re
    svc_url = re.sub(r"/[0-9]+$", "", layer_url.rstrip("/"))

    from qgis.PyQt.QtCore import QThread, pyqtSignal

    class _W(QThread):
        done = pyqtSignal(object)
        def __init__(self, fn, url):
            super().__init__()
            self.fn, self.url = fn, url
        def run(self):
            try:
                self.done.emit(self.fn(self.url, {"f": "json"}))
            except Exception:
                self.done.emit({})

    w = _W(client._get, svc_url)

    def _on_detail(detail):
        try:
            from qgis.core import QgsLayerMetadata, QgsCoordinateReferenceSystem, QgsBox3d
            md = layer.metadata()
            _apply_service_detail(md, detail, layer)
            layer.setMetadata(md)
        except Exception:
            pass

    w.done.connect(_on_detail)
    w.finished.connect(w.deleteLater)
    w.start()
    # Keep reference so thread isn't GC'd
    layer._agol_meta_worker = w
