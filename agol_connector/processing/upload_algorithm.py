"""
processing/upload_algorithm.py — Upload layer to ArcGIS Online
==============================================================
Appears in Processing Toolbox → ArcGIS Online Connector.
Supports batch processing, model integration, and history.

Parameters:
  INPUT          QgsMapLayerParameter  — vector or raster layer
  TITLE          string                — item title on AGOL
  DESCRIPTION    string (optional)     — item description
  TAGS           string                — comma-separated tags
  CONNECTION     string                — connection name from CredentialStore
  FOLDER         string (optional)     — destination folder name
  GROUP          string (optional)     — group name to share with
"""

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputString,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsMapLayer,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsProject,
)
from qgis.PyQt.QtGui import QIcon
import os, json, tempfile


class AGOLUploadAlgorithm(QgsProcessingAlgorithm):

    INPUT       = "INPUT"
    TITLE       = "TITLE"
    DESCRIPTION = "DESCRIPTION"
    TAGS        = "TAGS"
    CONNECTION  = "CONNECTION"
    FOLDER      = "FOLDER"
    GROUP       = "GROUP"
    OUTPUT_URL  = "OUTPUT_URL"

    def name(self) -> str:
        return "uploadtolayer"

    def displayName(self) -> str:
        return "Upload layer to ArcGIS Online"

    def group(self) -> str:
        return "Data management"

    def groupId(self) -> str:
        return "datamanagement"

    def shortHelpString(self) -> str:
        return (
            "Upload a vector or raster layer to ArcGIS Online as a hosted "
            "Feature Service or Image Service.\n\n"
            "The layer is exported to GeoJSON (vector) or GeoTIFF (raster) "
            "and uploaded via the AGOL REST API.\n\n"
            "Requires an active connection configured in "
            "AGOL Connector → Connections."
        )

    def icon(self) -> QIcon:
        path = os.path.join(
            os.path.dirname(__file__), "..", "resources", "icon_upload.png"
        )
        return QIcon(path) if os.path.exists(path) else QIcon()

    def initAlgorithm(self, config=None):
        from ..credentials import CredentialStore
        conn_names = CredentialStore.instance().connection_names()

        self.addParameter(
            QgsProcessingParameterMapLayer(self.INPUT, "Input layer")
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.TITLE, "Title on ArcGIS Online", defaultValue=""
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.DESCRIPTION, "Description", defaultValue="",
                optional=True, multiLine=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.TAGS, "Tags (comma-separated)",
                defaultValue="qgis,upload", optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.CONNECTION, "Connection",
                options=conn_names if conn_names else ["(no connections)"],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FOLDER, "Destination folder (leave blank for Home)",
                defaultValue="", optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.GROUP, "Share with group (leave blank to skip)",
                defaultValue="", optional=True,
            )
        )
        self.addOutput(
            QgsProcessingOutputString(self.OUTPUT_URL, "Service URL")
        )

    def processAlgorithm(self, parameters, context, feedback):
        from ..credentials import CredentialStore
        from ..agol_client import AGOLClient

        # ── Resolve connection ─────────────────────────────────────────
        store       = CredentialStore.instance()
        conn_names  = store.connection_names()
        conn_idx    = self.parameterAsEnum(parameters, self.CONNECTION, context)
        conn_name   = conn_names[conn_idx] if conn_names else ""

        client = store.get_client(conn_name)
        if not client:
            # Try silent re-auth with saved credentials
            creds = store._load_auth_config(conn_name)
            if creds and creds.get("username"):
                url = store.connection_url(conn_name)
                client = AGOLClient(portal_url=url)
                try:
                    client.login_token(
                        creds["username"], creds["password"], referer=url
                    )
                    store.set_client(conn_name, client)
                except Exception as e:
                    raise Exception(
                        f"Not signed in to '{conn_name}' and auto-authentication "
                        f"failed: {e}\n\nPlease sign in via AGOL Connector → "
                        f"Connections before running this algorithm."
                    )
            else:
                raise Exception(
                    f"Not signed in to '{conn_name}'.\n\n"
                    "Please sign in via AGOL Connector → Connections first."
                )

        # ── Parameters ────────────────────────────────────────────────
        layer       = self.parameterAsLayer(parameters, self.INPUT, context)
        title       = self.parameterAsString(parameters, self.TITLE, context).strip()
        description = self.parameterAsString(parameters, self.DESCRIPTION, context)
        tags        = self.parameterAsString(parameters, self.TAGS, context) or "qgis"
        folder_name = self.parameterAsString(parameters, self.FOLDER, context).strip()
        group_name  = self.parameterAsString(parameters, self.GROUP, context).strip()

        if not title:
            title = layer.name()

        # ── Resolve folder ID ─────────────────────────────────────────
        folder_id = ""
        if folder_name:
            feedback.pushInfo(f"Resolving folder '{folder_name}'…")
            folders = client.get_user_folders()
            for f in folders:
                if f.get("title", "").lower() == folder_name.lower():
                    folder_id = f.get("id", "")
                    break
            if not folder_id:
                raise Exception(
                    f"Folder '{folder_name}' not found on ArcGIS Online. "
                    "Create it first or leave blank to use Home."
                )

        # ── Resolve group ID ──────────────────────────────────────────
        group_id = ""
        if group_name:
            feedback.pushInfo(f"Resolving group '{group_name}'…")
            groups = client.get_user_groups()
            for g in groups:
                if g.get("title", "").lower() == group_name.lower():
                    group_id = g.get("id", "")
                    break
            if not group_id:
                feedback.pushWarning(
                    f"Group '{group_name}' not found — uploading without sharing."
                )

        # ── Upload ────────────────────────────────────────────────────
        if feedback.isCanceled():
            return {}

        if layer.type() == QgsMapLayer.LayerType.VectorLayer:
            result = self._upload_vector(
                layer, client, title, description, tags,
                folder_id, group_id, feedback,
            )
        else:
            result = self._upload_raster(
                layer, client, title, description, tags,
                folder_id, group_id, feedback,
            )

        service_url = result.get("serviceurl", result.get("encodedServiceURL", ""))
        feedback.pushInfo(f"Upload complete. Service URL: {service_url}")

        # Share with group if requested
        if group_id:
            item_id = result.get("_item_id", "")
            if item_id:
                try:
                    client.add_item_to_group(item_id, group_id)
                    feedback.pushInfo(f"Shared with group '{group_name}'.")
                except Exception as e:
                    feedback.pushWarning(f"Could not share with group: {e}")

        return {self.OUTPUT_URL: service_url}

    def _upload_vector(self, layer, client, title, desc, tags,
                        folder_id, group_id, feedback):
        feedback.pushInfo("Exporting layer to GeoJSON…")
        feedback.setProgress(10)

        tmp = tempfile.NamedTemporaryFile(
            suffix=".geojson", delete=False, mode="w", encoding="utf-8"
        )
        tmp.close()
        try:
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, tmp.name,
                layer.transformContext(),
                QgsVectorFileWriter.SaveVectorOptions(),
            )
            if err != QgsVectorFileWriter.WriterError.NoError:
                raise Exception(f"Export failed: {msg}")

            feedback.pushInfo("Uploading to ArcGIS Online…")
            feedback.setProgress(40)

            with open(tmp.name, encoding="utf-8") as f:
                geojson = json.load(f)

            result = client.upload_geojson_as_service(
                title, geojson,
                description=desc, tags=tags, folder_id=folder_id,
            )
            feedback.setProgress(100)
            return result
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    def _upload_raster(self, layer, client, title, desc, tags,
                        folder_id, group_id, feedback):
        src_path = layer.source().split("|")[0]
        if not os.path.exists(src_path):
            raise Exception(
                "Cannot find raster file on disk. "
                "Only file-based rasters can be uploaded."
            )
        feedback.pushInfo(f"Uploading raster '{src_path}'…")
        feedback.setProgress(20)
        result = client.upload_raster(
            title, src_path,
            description=desc, tags=tags, folder_id=folder_id,
        )
        feedback.setProgress(100)
        return result

    def createInstance(self):
        return AGOLUploadAlgorithm()

    def flags(self):
        # FlagSupportsBatch enables batch processing in the toolbox
        try:
            return super().flags() | QgsProcessingAlgorithm.Flag.FlagSupportsBatch
        except AttributeError:
            return super().flags()
