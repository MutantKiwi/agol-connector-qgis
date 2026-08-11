"""
dialogs/agol_properties_dialog.py
==================================
Service / layer properties dialog populated from AGOL REST metadata.

Tabs:
  Info          — pulled from the live feature service REST endpoint
  Fields        — field name / type / alias / length table
  Capabilities  — essential QGIS-relevant items, MultiPatch warning in red
  Extent        — coordinates + zoomable QgsMapCanvas with +/- buttons

CRS labels show full name + EPSG link, e.g.
  EPSG:7850 — GDA2020 / MGA zone 50  [link to epsg.io/7850]
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QFormLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QDialogButtonBox, QTextEdit, QSizePolicy,
    QPushButton, QAbstractItemView, QGroupBox, QScrollArea,
    QFrame,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QFont, QDesktopServices

from ..agol_client import AGOLClient


# ── Worker ─────────────────────────────────────────────────────────────────

class _Worker(QThread):
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


# ── CRS helper ──────────────────────────────────────────────────────────────

def _crs_label_html(wkid: int) -> str:
    """Return HTML with CRS name and clickable epsg.io link."""
    name = ""
    try:
        from qgis.core import QgsCoordinateReferenceSystem
        crs = QgsCoordinateReferenceSystem(f"EPSG:{wkid}")
        if crs.isValid():
            name = crs.description()
    except Exception:
        pass
    link = f"https://epsg.io/{wkid}"
    if name:
        return (f'EPSG:{wkid} — {name} '
                f'<a href="{link}">[epsg.io]</a>')
    return f'EPSG:{wkid} <a href="{link}">[epsg.io]</a>'


# ── Constants: QGIS-essential capabilities ─────────────────────────────────

_ESSENTIAL_CAPS = {
    # cap key        display label
    "Query":         "Query features",
    "Create":        "Create features",
    "Update":        "Edit features",
    "Delete":        "Delete features",
    "Extract":       "Extract / export",
    "Sync":          "Sync",
}

_ESSENTIAL_ADV = {
    "Pagination":               "Pagination (resultOffset)",
    "DefaultSR":                "Custom output CRS (outSR)",
    "Distinct":                 "Distinct values",
    "Statistics":               "Server-side statistics",
    "OrderBy":                  "Server-side sort (ORDER BY)",
    "SqlExpression":            "Full SQL expressions",
    "HavingClause":             "HAVING clause",
    "QueryWithDistance":        "Distance filter",
    "ReturningQueryExtent":     "Return extent only",
    "TopFeaturesQuery":         "TOP N features",
    "CurrentUserQueries":       "Current user filter",
    "QueryRelatedPagination":   "Related table pagination",
}


# ── Main dialog ────────────────────────────────────────────────────────────

class AGOLPropertiesDialog(QDialog):

    def __init__(self, item_meta: dict, layer_url: str,
                 service_kind: str, client: AGOLClient, parent=None):
        super().__init__(parent)
        self._meta   = item_meta
        self._url    = layer_url
        self._kind   = service_kind
        self._client = client
        self._worker = None
        self._map_canvas = None
        self._detail: dict = {}

        title = item_meta.get("title") or item_meta.get("name", "Properties")
        self.setWindowTitle(f"Properties — {title}")
        self.setMinimumSize(600, 560)
        self._build_ui()
        self._populate_from_meta(item_meta)
        if layer_url:
            self._fetch_detail()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.tabs.addTab(self._build_info_tab(),   "Info")
        self.tabs.addTab(self._build_fields_tab(), "Fields")
        self.tabs.addTab(self._build_caps_tab(),   "Capabilities")
        self.tabs.addTab(self._build_extent_tab(), "Extent")

        layout.addWidget(self.tabs)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid); font-size: 11px;")
        self._status.setVisible(False)
        layout.addWidget(self._status)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ── Info tab — populated from live service JSON ──────────────────────

    def _build_info_tab(self) -> QWidget:
        """Scrollable form populated from the feature service REST endpoint."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        inner = QWidget()
        f = QFormLayout(inner)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        f.setSpacing(6)
        f.setContentsMargins(8, 8, 8, 8)

        def _val(url_mode=False):
            l = QLabel("—")
            l.setWordWrap(True)
            if url_mode:
                l.setOpenExternalLinks(True)
                l.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextBrowserInteraction
                )
            else:
                l.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
            return l

        self._i_title     = _val()
        self._i_owner     = _val()
        self._i_type      = _val()
        self._i_geom_type = _val()
        self._i_modified  = _val()
        self._i_svc_type  = _val()
        self._i_svc_ver   = _val()
        self._i_tags      = _val()
        self._i_snippet   = _val()
        self._i_url       = _val(url_mode=True)
        self._i_item_id   = _val()
        self._i_max_rec   = _val()
        self._i_has_att   = _val()
        self._i_sync      = _val()
        self._i_copyright = _val()

        rows = [
            ("Title",           self._i_title),
            ("Owner",           self._i_owner),
            ("Item type",       self._i_type),
            ("Geometry type",   self._i_geom_type),
            ("Service type",    self._i_svc_type),
            ("Service version", self._i_svc_ver),
            ("Max record count",self._i_max_rec),
            ("Attachments",     self._i_has_att),
            ("Sync",            self._i_sync),
            ("Modified",        self._i_modified),
            ("Copyright",       self._i_copyright),
            ("Tags",            self._i_tags),
            ("Description",     self._i_snippet),
            ("Service URL",     self._i_url),
            ("Item ID",         self._i_item_id),
        ]
        for lbl, widget in rows:
            bold = QLabel(f"<b>{lbl}</b>")
            f.addRow(bold, widget)

        scroll.setWidget(inner)
        return scroll

    # ── Fields tab ──────────────────────────────────────────────────────

    def _build_fields_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self._fields_note = QLabel(
            "Field information loads from the service REST endpoint."
        )
        self._fields_note.setStyleSheet("color: palette(mid); font-size: 11px;")
        v.addWidget(self._fields_note)

        self._fields_tbl = QTableWidget(0, 5)
        self._fields_tbl.setHorizontalHeaderLabels(
            ["Name", "Type", "Alias", "Length", "Nullable"]
        )
        hdr = self._fields_tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._fields_tbl.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._fields_tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._fields_tbl.setAlternatingRowColors(True)
        v.addWidget(self._fields_tbl)
        return w

    # ── Capabilities tab ────────────────────────────────────────────────

    def _build_caps_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(10)

        # Warning label (hidden unless MultiPatch)
        self._caps_warning = QLabel()
        self._caps_warning.setStyleSheet(
            "color: #cc0000; font-weight: bold; "
            "background: #fff0f0; border: 1px solid #cc0000; "
            "border-radius: 4px; padding: 6px;"
        )
        self._caps_warning.setWordWrap(True)
        self._caps_warning.setVisible(False)
        v.addWidget(self._caps_warning)

        # Essential section
        ess_box = QGroupBox("Essential (QGIS relevant)")
        self._ess_form = QFormLayout(ess_box)
        self._ess_form.setSpacing(4)
        v.addWidget(ess_box)

        # Limits section
        lim_box = QGroupBox("Limits")
        self._lim_form = QFormLayout(lim_box)
        self._lim_form.setSpacing(4)
        v.addWidget(lim_box)

        # Advanced query section
        adv_box = QGroupBox("Advanced query capabilities")
        self._adv_form = QFormLayout(adv_box)
        self._adv_form.setSpacing(4)
        v.addWidget(adv_box)

        # Service info section
        svc_box = QGroupBox("Service information")
        self._svc_form = QFormLayout(svc_box)
        self._svc_form.setSpacing(4)
        v.addWidget(svc_box)

        v.addStretch()
        scroll.setWidget(inner)
        return scroll

    # ── Extent tab ──────────────────────────────────────────────────────

    def _build_extent_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)

        # Coordinate form
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f.setSpacing(4)

        def _coord_lbl():
            l = QLabel("—")
            l.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            return l

        def _crs_lbl():
            l = QLabel("—")
            l.setOpenExternalLinks(True)
            l.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
            return l

        self._e_xmin = _coord_lbl()
        self._e_ymin = _coord_lbl()
        self._e_xmax = _coord_lbl()
        self._e_ymax = _coord_lbl()
        self._e_crs  = _crs_lbl()
        f.addRow("<b>X min</b>", self._e_xmin)
        f.addRow("<b>Y min</b>", self._e_ymin)
        f.addRow("<b>X max</b>", self._e_xmax)
        f.addRow("<b>Y max</b>", self._e_ymax)
        f.addRow("<b>CRS</b>",   self._e_crs)
        # Fix bold labels
        for i in range(f.rowCount()):
            lbl_item = f.itemAt(i, QFormLayout.ItemRole.LabelRole)
            if lbl_item and lbl_item.widget():
                lbl_item.widget().setTextFormat(Qt.TextFormat.RichText)
        v.addLayout(f)

        # Zoom buttons row + placeholder/canvas
        btn_row = QHBoxLayout()
        self._zoom_in_btn  = QPushButton("+")
        self._zoom_out_btn = QPushButton("−")
        self._zoom_fit_btn = QPushButton("⌖")
        for b, tip in [(self._zoom_in_btn,  "Zoom in"),
                        (self._zoom_out_btn, "Zoom out"),
                        (self._zoom_fit_btn, "Fit extent")]:
            b.setFixedSize(28, 28)
            b.setToolTip(tip)
            b.setEnabled(False)
            btn_row.addWidget(b)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._map_placeholder = QLabel("Select this tab to load the extent map.")
        self._map_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._map_placeholder.setMinimumHeight(240)
        self._map_placeholder.setStyleSheet(
            "background: palette(base); color: palette(mid); "
            "border: 1px solid palette(mid);"
        )
        v.addWidget(self._map_placeholder)

        self._pending_extent = None
        return w

    # ── Populate from item metadata (immediate) ─────────────────────────

    def _populate_from_meta(self, meta: dict):
        self._i_title.setText(meta.get("title", "—"))
        self._i_owner.setText(meta.get("owner", "—"))
        self._i_type.setText(meta.get("type", "—"))
        self._i_item_id.setText(meta.get("id", "—"))

        ms = meta.get("modified", 0)
        if ms:
            from datetime import datetime, timezone
            d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            self._i_modified.setText(d.strftime("%Y-%m-%d  %H:%M UTC"))
        else:
            self._i_modified.setText("—")

        tags = meta.get("tags") or meta.get("typeKeywords") or []
        self._i_tags.setText(
            ", ".join(tags) if isinstance(tags, list) else str(tags) or "—"
        )
        self._i_snippet.setText(
            meta.get("snippet") or meta.get("description") or "—"
        )
        raw_url = meta.get("url", "")
        if raw_url:
            self._i_url.setText(f'<a href="{raw_url}">{raw_url}</a>')
        else:
            self._i_url.setText("—")

        bbox = meta.get("extent")
        if bbox and len(bbox) >= 2:
            try:
                self._e_xmin.setText(str(bbox[0][0]))
                self._e_ymin.setText(str(bbox[0][1]))
                self._e_xmax.setText(str(bbox[1][0]))
                self._e_ymax.setText(str(bbox[1][1]))
                self._e_crs.setText(_crs_label_html(4326))
                self._pending_extent = {
                    "xmin": bbox[0][0], "ymin": bbox[0][1],
                    "xmax": bbox[1][0], "ymax": bbox[1][1],
                    "wkid": 4326,
                }
            except Exception:
                pass

    # ── Async detail fetch ──────────────────────────────────────────────

    def _fetch_detail(self):
        self._status.setText("Loading service details…")
        self._status.setVisible(True)
        self._worker = _Worker(self._client._get, self._url, {"f": "json"})
        self._worker.result.connect(self._on_detail)
        self._worker.error.connect(
            lambda e: (self._status.setText(f"Could not load detail: {e}"),
                       self._status.setVisible(True))
        )
        self._worker.finished.connect(lambda: self._status.setVisible(False))
        self._worker.start()

    def _on_detail(self, detail: dict):
        self._detail = detail
        self._status.setVisible(False)
        self._populate_info_from_detail(detail)
        self._populate_fields(detail)
        self._populate_capabilities(detail)
        self._populate_extent_coords(detail)

    # ── Info from service JSON ──────────────────────────────────────────

    def _populate_info_from_detail(self, detail: dict):
        """Override item-meta values with richer data from service JSON."""
        # Geometry type — strip esriGeometry prefix
        geom = detail.get("geometryType", "")
        self._i_geom_type.setText(
            geom.replace("esriGeometry", "") if geom else "—"
        )
        # Service type / data type
        stype = detail.get("type") or detail.get("serviceDataType", "")
        self._i_svc_type.setText(stype or "—")
        # Service version
        ver = detail.get("currentVersion")
        self._i_svc_ver.setText(str(ver) if ver else "—")
        # Max record count
        max_rec = detail.get("maxRecordCount")
        self._i_max_rec.setText(f"{int(max_rec):,}" if max_rec else "—")
        # Attachments
        has_att = detail.get("hasAttachments")
        self._i_has_att.setText("Yes" if has_att else "No" if has_att is not None else "—")
        # Sync
        sync = detail.get("syncEnabled")
        self._i_sync.setText("Enabled" if sync else "Disabled" if sync is not None else "—")
        # Copyright
        cr = detail.get("copyrightText", "")
        self._i_copyright.setText(cr or "—")

    # ── Fields ──────────────────────────────────────────────────────────

    def _populate_fields(self, detail: dict):
        fields = detail.get("fields", [])
        if not fields:
            self._fields_note.setText("No field information returned by this service.")
            return
        self._fields_note.setVisible(False)
        self._fields_tbl.setRowCount(len(fields))
        for row, f in enumerate(fields):
            friendly = f.get("type", "").replace("esriFieldType", "")
            nullable = "Yes" if f.get("nullable", True) else "No"
            length   = str(f.get("length", "")) if f.get("length") else ""
            for col, val in enumerate([
                f.get("name", ""),
                friendly,
                f.get("alias", f.get("name", "")),
                length,
                nullable,
            ]):
                it = QTableWidgetItem(val)
                it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._fields_tbl.setItem(row, col, it)

    # ── Capabilities ────────────────────────────────────────────────────

    def _populate_capabilities(self, detail: dict):
        def _tick(yes): return "✔" if yes else "✘"
        def _row(form, label, value, warn=False):
            lbl = QLabel(f"<b>{label}</b>")
            val = QLabel(str(value))
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if warn:
                val.setStyleSheet("color: #cc0000; font-weight: bold;")
            form.addRow(lbl, val)

        caps_str = detail.get("capabilities", "")
        caps_set  = {c.strip() for c in caps_str.split(",")} if caps_str else set()

        # MultiPatch warning
        geom = detail.get("geometryType", "")
        if geom == "esriGeometryMultiPatch":
            self._caps_warning.setText(
                "⚠  MultiPatch geometry detected.\n"
                "This is a 3D surface type (buildings, terrain). QGIS will load "
                "it as a polygon layer but Z values and 3D structure will be lost."
            )
            self._caps_warning.setVisible(True)

        # Essential capabilities
        for cap_key, cap_label in _ESSENTIAL_CAPS.items():
            has = cap_key in caps_set
            _row(self._ess_form, cap_label, _tick(has) + ("" if has else "  (not available)"),
                 warn=(not has and cap_key == "Query"))

        # Limits
        max_rec = detail.get("maxRecordCount")
        if max_rec is not None:
            warn_small = int(max_rec) < 500
            _row(self._lim_form, "Max record count",
                 f"{int(max_rec):,}  {'⚠ low — pagination essential' if warn_small else ''}",
                 warn=warn_small)
        exts = detail.get("supportedExtensions", "")
        if exts:
            _row(self._lim_form, "Extensions", exts)
        svc_ver = detail.get("currentVersion")
        if svc_ver:
            _row(self._lim_form, "Service version", str(svc_ver))

        # Advanced query capabilities
        adv = detail.get("advancedQueryCapabilities", {})
        for adv_key, adv_label in _ESSENTIAL_ADV.items():
            key_full = f"supports{adv_key}"
            if key_full in adv:
                has = bool(adv[key_full])
                _row(self._adv_form, adv_label, _tick(has))

        # Service info
        stype = detail.get("type") or detail.get("serviceDataType", "")
        if stype:
            _row(self._svc_form, "Service data type", stype)
        sync = detail.get("syncEnabled")
        if sync is not None:
            _row(self._svc_form, "Sync", "Enabled" if sync else "Disabled")
        has_att = detail.get("hasAttachments")
        if has_att is not None:
            _row(self._svc_form, "Attachments", "Yes" if has_att else "No")

    # ── Extent coords ────────────────────────────────────────────────────

    def _populate_extent_coords(self, detail: dict):
        ext = (detail.get("extent") or
               detail.get("fullExtent") or
               detail.get("initialExtent"))
        if not ext:
            return
        try:
            xmin = float(ext["xmin"])
            ymin = float(ext["ymin"])
            xmax = float(ext["xmax"])
            ymax = float(ext["ymax"])
            sr   = ext.get("spatialReference", {})
            if not isinstance(sr, dict):
                sr = {}
            wkid = int(sr.get("latestWkid") or sr.get("wkid") or 4326)

            self._e_xmin.setText(f"{xmin:.6f}")
            self._e_ymin.setText(f"{ymin:.6f}")
            self._e_xmax.setText(f"{xmax:.6f}")
            self._e_ymax.setText(f"{ymax:.6f}")
            self._e_crs.setText(_crs_label_html(wkid))

            self._pending_extent = {
                "xmin": xmin, "ymin": ymin,
                "xmax": xmax, "ymax": ymax,
                "wkid": wkid,
            }
            if self.tabs.currentIndex() == 3:
                self._init_map()
        except Exception:
            pass

    # ── Tab change → lazy map init ──────────────────────────────────────

    def _on_tab_changed(self, index: int):
        if index == 3 and self._map_canvas is None:
            QTimer.singleShot(100, self._init_map)

    def _init_map(self):
        if self._map_canvas is not None:
            return
        if not self._pending_extent:
            self._map_placeholder.setText("Extent not available for this service.")
            return
        try:
            self._build_map_canvas(self._pending_extent)
        except Exception as e:
            self._map_placeholder.setText(f"Map unavailable: {e}")

    def _build_map_canvas(self, ext: dict):
        from qgis.gui import QgsMapCanvas, QgsRubberBand
        from qgis.core import (
            QgsRasterLayer, QgsRectangle, QgsCoordinateReferenceSystem,
            QgsCoordinateTransform, QgsProject, QgsGeometry, QgsWkbTypes,
        )

        canvas = QgsMapCanvas()
        canvas.setMinimumHeight(240)
        canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        canvas.enableAntiAliasing(True)
        canvas.setCanvasColor(QColor(240, 240, 240))
        # Max zoom level 18
        canvas.setProperty("_agol_max_zoom", 18)

        basemap_uri = (
            "type=xyz"
            "&url=https://services.arcgisonline.com/ArcGIS/rest/services/"
            "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
            "&zmin=0&zmax=18"
            "&crs=EPSG:3857"
        )
        basemap = QgsRasterLayer(basemap_uri, "Basemap", "wms")

        map_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        src_crs = QgsCoordinateReferenceSystem(f"EPSG:{ext['wkid']}")
        if not src_crs.isValid():
            src_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        rect_src = QgsRectangle(ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"])
        if src_crs != map_crs:
            xf = QgsCoordinateTransform(src_crs, map_crs, QgsProject.instance())
            rect_map = xf.transformBoundingBox(rect_src)
        else:
            rect_map = rect_src

        self._rect_map = rect_map   # keep for fit-extent
        canvas.setDestinationCrs(map_crs)
        if basemap.isValid():
            canvas.setLayers([basemap])
        buf = max(rect_map.width(), rect_map.height()) * 0.25
        canvas.setExtent(rect_map.buffered(buf))

        try:
            rb = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        except AttributeError:
            rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        rb.setColor(QColor(220, 60, 0, 200))
        rb.setFillColor(QColor(220, 60, 0, 35))
        rb.setWidth(2)
        rb.setToGeometry(QgsGeometry.fromRect(rect_map), None)
        self._rubber_band = rb

        # CRS label overlay
        crs_text = f"EPSG:{ext['wkid']}"
        try:
            from qgis.core import QgsCoordinateReferenceSystem as _CRS
            c = _CRS(f"EPSG:{ext['wkid']}")
            if c.isValid():
                crs_text = f"EPSG:{ext['wkid']} — {c.description()}"
        except Exception:
            pass
        crs_label = QLabel(crs_text, canvas)
        crs_label.setStyleSheet(
            "background: rgba(255,255,255,200); color: #333; "
            "padding: 2px 6px; font-size: 11px; border-top-left-radius: 3px;"
        )
        crs_label.adjustSize()

        def _pos():
            crs_label.move(
                canvas.width()  - crs_label.width()  - 1,
                canvas.height() - crs_label.height() - 1,
            )
        canvas.installEventFilter(self)
        self._crs_label_overlay = crs_label
        self._pos_fn = _pos

        # Wire zoom buttons now that canvas exists
        self._zoom_in_btn.setEnabled(True)
        self._zoom_out_btn.setEnabled(True)
        self._zoom_fit_btn.setEnabled(True)
        self._zoom_in_btn.clicked.connect(
            lambda: canvas.zoomIn() or canvas.refresh()
        )
        self._zoom_out_btn.clicked.connect(
            lambda: canvas.zoomOut() or canvas.refresh()
        )
        self._zoom_fit_btn.clicked.connect(self._fit_extent)

        # Swap placeholder
        extent_tab = self.tabs.widget(3)
        layout = extent_tab.layout()
        layout.replaceWidget(self._map_placeholder, canvas)
        self._map_placeholder.hide()
        self._map_placeholder.deleteLater()

        self._map_canvas = canvas
        self._basemap    = basemap
        canvas.refresh()
        QTimer.singleShot(50, _pos)

    def _fit_extent(self):
        if self._map_canvas and hasattr(self, "_rect_map"):
            buf = max(self._rect_map.width(), self._rect_map.height()) * 0.25
            self._map_canvas.setExtent(self._rect_map.buffered(buf))
            self._map_canvas.refresh()

    def eventFilter(self, obj, event):
        from qgis.PyQt.QtCore import QEvent
        if (obj is self._map_canvas and
                event.type() == QEvent.Type.Resize and
                hasattr(self, "_pos_fn")):
            self._pos_fn()
        return super().eventFilter(obj, event)
