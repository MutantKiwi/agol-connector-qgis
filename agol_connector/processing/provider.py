"""
processing/provider.py — AGOL Connector Processing Provider
"""
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
import os

from .upload_algorithm import AGOLUploadAlgorithm


class AGOLProcessingProvider(QgsProcessingProvider):

    def id(self) -> str:
        return "agolconnector"

    def name(self) -> str:
        return "ArcGIS Online Connector"

    def longName(self) -> str:
        return self.name()

    def icon(self) -> QIcon:
        path = os.path.join(
            os.path.dirname(__file__), "..", "resources", "icon.png"
        )
        return QIcon(path) if os.path.exists(path) else QIcon()

    def loadAlgorithms(self):
        self.addAlgorithm(AGOLUploadAlgorithm())

    def supportedOutputRasterLayerExtensions(self):
        return []
