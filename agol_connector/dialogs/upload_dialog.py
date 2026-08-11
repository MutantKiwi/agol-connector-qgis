"""
dialogs/upload_dialog.py — Upload layer to AGOL
================================================
Supports:
  - Vector layers → hosted Feature Service (GeoJSON → addItem → publish)
  - Raster layers → hosted Image Service (file → addItem → publish)
  - Destination: user folder (existing or new)
  - Group sharing: add to existing group or create new group
"""

import os, json, tempfile
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QTextEdit,
    QPushButton, QProgressBar, QLabel, QDialogButtonBox,
    QMessageBox, QComboBox, QGroupBox, QHBoxLayout, QInputDialog,
    QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.core import QgsMapLayer, QgsVectorFileWriter, QgsVectorLayer

from ..agol_client import AGOLClient, AGOLRequestError


class _Worker(QThread):
    result  = pyqtSignal(object)
    error   = pyqtSignal(str)
    status  = pyqtSignal(str)
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
    def run(self):
        try:
            self.result.emit(self.fn(*self.args, **self.kwargs))
        except Exception as e:
            self.error.emit(str(e))


class UploadDialog(QDialog):

    def __init__(self, client: AGOLClient, layer: QgsMapLayer, parent=None):
        super().__init__(parent)
        self.client  = client
        self.layer   = layer
        self._folders: list[dict] = []
        self._groups:  list[dict] = []
        self._worker = None

        self.setWindowTitle(f"Upload — {layer.name()}")
        self.setMinimumWidth(460)
        self._build_ui()
        self._populate_connections()
        self._load_folders()
        self._load_groups()

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Connection selector
        conn_box = QGroupBox("Connection")
        cl = QHBoxLayout(conn_box)
        self.conn_combo = QComboBox()
        self.conn_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.conn_combo.currentIndexChanged.connect(self._on_connection_changed)
        cl.addWidget(self.conn_combo)
        layout.addWidget(conn_box)

        # Item details
        det_box = QGroupBox("Item details")
        df = QFormLayout(det_box)
        self.title_edit = QLineEdit(self.layer.name())
        df.addRow("Title", self.title_edit)

        self.summary_edit = QLineEdit()
        self.summary_edit.setPlaceholderText("Brief summary (shown in search results)")
        df.addRow("Summary", self.summary_edit)

        # Format + size info
        self._format_lbl = QLabel(self._get_format_info())
        self._format_lbl.setStyleSheet("color: palette(mid); font-size: 11px;")
        df.addRow("Format", self._format_lbl)
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Full description (supports HTML)")
        self.desc_edit.setMaximumHeight(60)
        df.addRow("Description", self.desc_edit)

        self.terms_edit = QTextEdit()
        self.terms_edit.setPlaceholderText("Terms of use / licence")
        self.terms_edit.setMaximumHeight(50)
        df.addRow("Terms of use", self.terms_edit)

        self.credits_edit = QLineEdit()
        self.credits_edit.setPlaceholderText("e.g. © Auckland Council 2024")
        df.addRow("Acknowledgements", self.credits_edit)

        self.tags_edit = QLineEdit("qgis,upload")
        df.addRow("Tags", self.tags_edit)
        layout.addWidget(det_box)

        # Destination folder
        folder_box = QGroupBox("Destination folder")
        fl = QHBoxLayout(folder_box)
        self.folder_combo = QComboBox()
        self.folder_combo.setMinimumWidth(180)
        fl.addWidget(self.folder_combo, 1)
        ref_btn = QPushButton("↺")
        ref_btn.setFixedWidth(28)
        ref_btn.setToolTip("Refresh folder list")
        ref_btn.clicked.connect(self._load_folders)
        fl.addWidget(ref_btn)
        new_btn = QPushButton("New folder…")
        new_btn.clicked.connect(self._create_folder)
        fl.addWidget(new_btn)
        layout.addWidget(folder_box)

        # Group sharing
        group_box = QGroupBox("Share with group (optional)")
        gl = QHBoxLayout(group_box)
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(180)
        self.group_combo.addItem("— no group —", "")
        gl.addWidget(self.group_combo, 1)
        gref_btn = QPushButton("↺")
        gref_btn.setFixedWidth(28)
        gref_btn.setToolTip("Refresh group list")
        gref_btn.clicked.connect(self._load_groups)
        gl.addWidget(gref_btn)
        gnew_btn = QPushButton("New group…")
        gnew_btn.clicked.connect(self._create_group)
        gl.addWidget(gnew_btn)
        layout.addWidget(group_box)

        # Sharing / access level
        from qgis.PyQt.QtWidgets import QRadioButton, QButtonGroup
        access_box = QGroupBox("Sharing")
        al = QHBoxLayout(access_box)
        self._access_group = QButtonGroup(self)
        for _label, _value, _tip in [
            ("Private", "private", "Only you can access"),
            ("Org",     "org",     "All members of your organisation"),
            ("Public",  "public",  "Anyone on the internet"),
        ]:
            rb = QRadioButton(_label)
            rb.setProperty("access_value", _value)
            rb.setToolTip(_tip)
            if _value == "private":
                rb.setChecked(True)
            self._access_group.addButton(rb)
            al.addWidget(rb)
        al.addStretch()
        layout.addWidget(access_box)


        # Progress
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setVisible(False)
        layout.addWidget(self.status_lbl)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Upload")
        self._upload_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self._upload)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ── Load folders / groups ────────────────────────────────────────────

    def _populate_connections(self):
        """Fill the connection combo from CredentialStore."""
        from ..credentials import CredentialStore
        store = CredentialStore.instance()
        self.conn_combo.blockSignals(True)
        self.conn_combo.clear()
        for name in store.connection_names():
            if store.get_client(name):
                self.conn_combo.addItem(name, name)
        # Select the connection matching current client
        for i in range(self.conn_combo.count()):
            c = store.get_client(self.conn_combo.itemData(i))
            if c is self.client:
                self.conn_combo.setCurrentIndex(i)
                break
        self.conn_combo.blockSignals(False)

    def _on_connection_changed(self, index: int):
        """Switch active client when user picks a different connection."""
        from ..credentials import CredentialStore
        name = self.conn_combo.currentData()
        if name:
            client = CredentialStore.instance().get_client(name)
            if client:
                self.client = client
                self._load_folders()
                self._load_groups()

    def _get_format_info(self) -> str:
        """Return a human-readable description of the upload format."""
        from qgis.core import QgsMapLayer
        if self.layer.type() == QgsMapLayer.LayerType.VectorLayer:
            fc = self.layer.featureCount()
            fc_str = f"{fc:,} features" if fc >= 0 else "unknown feature count"
            return (f"Vector → GeoJSON (or Shapefile ZIP if > 50 MB)  ·  {fc_str}")
        else:
            src_path = self.layer.source().split("|")[0]
            import os
            if os.path.exists(src_path):
                sz = os.path.getsize(src_path)
                sz_str = (f"{sz/1024/1024:.1f} MB" if sz > 1024*1024
                          else f"{sz/1024:.0f} KB")
                return f"Raster → file upload  ·  {sz_str}"
            return "Raster → file upload"

    def _load_folders(self):
        self.folder_combo.setEnabled(False)
        w = _Worker(self.client.get_user_folders)
        w.result.connect(self._populate_folders)
        w.error.connect(lambda e: self.folder_combo.setEnabled(True))
        w.finished.connect(w.deleteLater)
        w.start(); self._fw = w

    def _populate_folders(self, folders):
        self._folders = folders
        self.folder_combo.clear()
        self.folder_combo.addItem("Home (root)", "")
        for f in folders:
            self.folder_combo.addItem(f.get("title", "—"), f.get("id", ""))
        self.folder_combo.setEnabled(True)

    def _create_folder(self):
        name, ok = QInputDialog.getText(
            self, "New folder", "Folder name:"
        )
        if not ok or not name.strip():
            return
        try:
            result = self.client._post(
                f"{self.client.sharing}/content/users/"
                f"{self.client.username}/createFolder",
                {"title": name.strip(), "f": "json"},
            )
            if "folder" in result:
                self._load_folders()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _load_groups(self):
        w = _Worker(self.client.get_user_groups)
        w.result.connect(self._populate_groups)
        w.error.connect(lambda _: None)
        w.finished.connect(w.deleteLater)
        w.start(); self._gw = w

    def _populate_groups(self, groups):
        self._groups = groups
        current = self.group_combo.currentData()
        self.group_combo.clear()
        self.group_combo.addItem("— no group —", "")
        for g in groups:
            self.group_combo.addItem(g.get("title", "—"), g.get("id", ""))
        # Restore selection if possible
        idx = self.group_combo.findData(current)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)

    def _create_group(self):
        name, ok = QInputDialog.getText(
            self, "New group", "Group name:"
        )
        if not ok or not name.strip():
            return
        desc, ok2 = QInputDialog.getText(
            self, "New group", "Description (optional):"
        )
        try:
            self._set_status("Creating group…")
            group = self.client.create_group(
                name.strip(),
                description=desc.strip() if ok2 else "",
                access="private",
            )
            self._set_status("")
            self._load_groups()
            QMessageBox.information(
                self, "Group created",
                f"Group '{name.strip()}' created.\n"
                "Access: private — edit in AGOL to change."
            )
        except Exception as e:
            self._set_status("")
            QMessageBox.critical(self, "Error", str(e))

    # ── Upload ───────────────────────────────────────────────────────────

    def _upload(self):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Missing title", "Please enter a title.")
            return
        self._upload_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._set_status("Preparing layer…")

        folder_id = self.folder_combo.currentData() or ""
        group_id  = self.group_combo.currentData()  or ""
        tags      = self.tags_edit.text().strip() or "qgis"
        desc      = self.desc_edit.toPlainText().strip()
        summary   = self.summary_edit.text().strip()
        terms     = self.terms_edit.toPlainText().strip()
        credits   = self.credits_edit.text().strip()
        # Capture access in main thread — Qt widgets must not be read from worker threads
        access    = self._get_selected_access()

        if self.layer.type() == QgsMapLayer.LayerType.VectorLayer:
            w = _Worker(
                self._upload_vector,
                title, desc, tags, folder_id, group_id, access,
                summary, terms, credits,
            )
        else:
            w = _Worker(
                self._upload_raster,
                title, desc, tags, folder_id, group_id, access,
                summary, terms, credits,
            )
        w.status.connect(self._set_status)
        w.result.connect(self._on_upload_done)
        w.error.connect(self._on_upload_error)
        w.finished.connect(w.deleteLater)
        w.start()
        self._worker = w

    def _upload_vector(self, title, desc, tags, folder_id, group_id, access='private',
                       summary='', terms='', credits=''):
        from ..logger import AGOLLogger
        _log = AGOLLogger.instance()

        _log.info("Upload started", layer=self.layer.name(), access=access,
                  features=self.layer.featureCount(),
                  source=self.layer.source()[:120])

        tmp_path = None
        try:
            # If the source is already a local GeoJSON file, use it directly
            src = self.layer.source().split("|")[0]
            if (src.lower().endswith(".geojson") and
                    os.path.exists(src) and
                    os.path.getsize(src) > 0):
                _log.info("Using existing GeoJSON source",
                          size_mb=f"{os.path.getsize(src)/1024/1024:.1f} MB")
                result = self.client.upload_geojson_file(
                    title, src, description=desc, tags=tags,
                    folder_id=folder_id, summary=summary,
                    terms=terms, credits=credits,
                )
                if group_id:
                    self.client.add_item_to_group(result.get("_item_id",""), group_id)
                return result

            # Export to GeoJSON
            _log.info("Exporting to GeoJSON", features=self.layer.featureCount())
            tmp_path = tempfile.mktemp(suffix=".geojson")

            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GeoJSON"
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                self.layer, tmp_path,
                self.layer.transformContext(), opts,
            )
            _log.debug("Export result", err=str(err), msg=msg)

            if err != QgsVectorFileWriter.WriterError.NoError:
                raise AGOLRequestError(f"Could not export layer: {msg}")

            file_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            _log.info("Export complete", size_mb=f"{file_size/1024/1024:.1f} MB")

            if file_size == 0:
                raise AGOLRequestError(
                    "GeoJSON export produced 0 bytes. The layer source may no "
                    "longer be available. Try re-loading the layer before uploading."
                )

            result = self.client.upload_geojson_file(
                title, tmp_path, description=desc, tags=tags,
                folder_id=folder_id, summary=summary,
                terms=terms, credits=credits,
            )
            item_id_w = result.get("_item_id","")
            if group_id:
                self.client.add_item_to_group(item_id_w, group_id)
            if access != "private" and item_id_w:
                self.client.set_item_access(item_id_w, access)
            return result

        except Exception as e:
            _log.error("Upload vector failed", error=str(e))
            raise
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except Exception: pass


    def _upload_raster(self, title, desc, tags, folder_id, group_id, access='private',
                        summary='', terms='', credits=''):
        src_path = self.layer.source().split("|")[0]
        if not os.path.exists(src_path):
            raise AGOLRequestError(
                "Cannot find the raster file on disk.\n"
                "Only file-based rasters can be uploaded directly."
            )
        result = self.client.upload_raster(
            title, src_path,
            description=desc, tags=tags, folder_id=folder_id,
        )
        item_id = result.get("_item_id", "")
        if group_id and item_id:
            self.client.add_item_to_group(item_id, group_id)
        access = self._get_selected_access()
        if access != "private" and item_id:
            self.client.set_item_access(item_id, access,
                                         group_ids=group_id or "")
        return result


    def _on_upload_done(self, result):
        self.progress.setVisible(False)
        self._set_status("Upload complete.")
        self._upload_btn.setEnabled(True)

        result        = result or {}
        item_id       = result.get("_item_id", "")
        service_url   = result.get("serviceurl", result.get("encodedServiceURL", ""))

        # Build portal item page URL — more useful than the REST endpoint
        portal_base = getattr(self.client, "portal_url",
                              "https://www.arcgis.com").rstrip("/")
        # Convert API URL to web URL:
        # https://www.arcgis.com → https://www.arcgis.com/home/item.html?id=...
        # https://org.maps.arcgis.com → https://org.maps.arcgis.com/home/item.html?id=...
        item_page_url = (f"{portal_base}/home/item.html?id={item_id}#overview"
                         if item_id else "")

        from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        from qgis.PyQt.QtCore import Qt

        dlg = QDialog(self)
        dlg.setWindowTitle("Upload complete")
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)

        msg_lbl = QLabel("Layer uploaded successfully.")
        msg_lbl.setWordWrap(True)
        lay.addWidget(msg_lbl)

        if item_page_url:
            item_lbl = QLabel(
                f"<br><b>Item page</b><br>"
                f'<a href="{item_page_url}">{item_page_url}</a>'
            )
            item_lbl.setOpenExternalLinks(True)
            item_lbl.setWordWrap(True)
            item_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
            lay.addWidget(item_lbl)

        if service_url:
            svc_lbl = QLabel(
                f"<br><b>Service URL</b><br>"
                f'<a href="{service_url}">{service_url}</a>'
            )
            svc_lbl.setOpenExternalLinks(True)
            svc_lbl.setWordWrap(True)
            svc_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
            lay.addWidget(svc_lbl)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)

        dlg.exec()
        self.accept()

    def _on_upload_error(self, msg):
        self.progress.setVisible(False)
        self._set_status("")
        self._upload_btn.setEnabled(True)
        QMessageBox.critical(self, "Upload failed", msg)

    def _get_selected_access(self) -> str:
        """Return selected access level: private / org / public."""
        try:
            for btn in self._access_group.buttons():
                if btn.isChecked():
                    return btn.property("access_value")
        except Exception:
            pass
        return "private"

    def _set_status(self, msg: str):
        self.status_lbl.setText(msg)
        self.status_lbl.setVisible(bool(msg))
