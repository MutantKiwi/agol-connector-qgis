"""AGOL Connector — QGIS plugin init."""


def classFactory(iface):
    from .agol_connector import AGOLConnector
    return AGOLConnector(iface)
