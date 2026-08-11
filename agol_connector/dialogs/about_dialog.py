"""
dialogs/about_dialog.py — About AGOL Connector
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QDialogButtonBox, QFrame,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap, QFont, QDesktopServices
from qgis.PyQt.QtCore import QUrl
import os

_VERSION = "1.0.0"


class AboutDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About AGOL Connector")
        self.setMinimumWidth(420)
        self.setMaximumWidth(520)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Logo + title ─────────────────────────────────────────────────
        header = QHBoxLayout()
        icon_path = os.path.join(
            os.path.dirname(__file__), "..", "resources", "icon_agol_root.png"
        )
        if os.path.exists(icon_path):
            logo = QLabel()
            pix = QPixmap(icon_path).scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo.setPixmap(pix)
            header.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        t = QLabel("AGOL Connector")
        f = QFont()
        f.setPointSize(14)
        f.setWeight(QFont.Weight.Bold)
        t.setFont(f)
        title_col.addWidget(t)

        ver = QLabel(f"Version {_VERSION}")
        ver.setStyleSheet("color: palette(mid);")
        title_col.addWidget(ver)

        tagline = QLabel("Connect QGIS to ArcGIS Online via the public REST API")
        tagline.setWordWrap(True)
        title_col.addWidget(tagline)

        header.addLayout(title_col)
        header.addStretch()
        layout.addLayout(header)

        # ── Separator ────────────────────────────────────────────────────
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: palette(mid);")
        layout.addWidget(line)

        # ── Description ──────────────────────────────────────────────────
        desc = QLabel(
            "Browse, search and load ArcGIS Online feature services, map services, "
            "image services, tile layers and web maps directly into QGIS. "
            "Upload vector and raster layers to ArcGIS Online as hosted services.\n\n"
            "No Esri libraries required — uses the public AGOL REST API only."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("color: palette(mid);")
        layout.addWidget(line2)

        # ── Details table ─────────────────────────────────────────────────
        def _row(label: str, value: str, link: str = ""):
            row = QHBoxLayout()
            lbl = QLabel(f"<b>{label}</b>")
            lbl.setFixedWidth(80)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            row.addWidget(lbl)
            if link:
                val = QLabel(f'<a href="{link}">{value}</a>')
                val.setOpenExternalLinks(True)
            else:
                val = QLabel(value)
            val.setWordWrap(True)
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse |
                Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            row.addWidget(val, 1)
            return row

        layout.addLayout(_row("Version",     _VERSION))
        layout.addLayout(_row("Author",      "mutant.kiwi  ·  hello@mutant.kiwi"))
        layout.addLayout(_row(
            "Source code",
            "github.com/MutantKiwi/agol-connector-qgis",
            "https://github.com/MutantKiwi/agol-connector-qgis",
        ))
        layout.addLayout(_row(
            "Bug tracker",
            "github.com/MutantKiwi/agol-connector-qgis/issues",
            "https://github.com/MutantKiwi/agol-connector-qgis/issues",
        ))
        layout.addLayout(_row(
            "Licence",
            "GNU General Public License v3.0",
            "https://www.gnu.org/licenses/gpl-3.0.html",
        ))
        layout.addLayout(_row(
            "Supported",
            "Feature Services · Map Services · Image Services · "
            "Tile Layers · Vector Tiles · Web Maps",
        ))
        layout.addLayout(_row(
            "Auth",
            "Username/password (generateToken) · OAuth2 / SSO · "
            "Encrypted via QGIS Authentication Manager",
        ))

        layout.addStretch()

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)
