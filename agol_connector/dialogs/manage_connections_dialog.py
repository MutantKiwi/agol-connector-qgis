"""
dialogs/manage_connections_dialog.py
Simple list of saved connections with Add / Edit / Remove / Connect buttons.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QDialogButtonBox, QLabel, QMessageBox,
)
from qgis.PyQt.QtCore import Qt

from ..credentials import CredentialStore
from .connection_dialog import ConnectionDialog


class ManageConnectionsDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AGOL connections")
        self.setMinimumSize(400, 300)
        self._store = CredentialStore.instance()
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Saved connections:"))

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._add_btn  = QPushButton("Add…")
        self._edit_btn = QPushButton("Edit…")
        self._del_btn  = QPushButton("Remove")
        self._con_btn  = QPushButton("Connect")
        for b in (self._add_btn, self._edit_btn, self._del_btn, self._con_btn):
            btn_row.addWidget(b)
        self._add_btn.clicked.connect(self._add)
        self._edit_btn.clicked.connect(self._edit)
        self._del_btn.clicked.connect(self._remove)
        self._con_btn.clicked.connect(self._connect)
        layout.addLayout(btn_row)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self._update_buttons()

    def _refresh(self):
        self._list.clear()
        for name in self._store.connection_names():
            url    = self._store.connection_url(name)
            client = self._store.get_client(name)
            if client and client.username:
                label = f"● {name}  —  {client.username}  ({url})"
            else:
                label = f"○ {name}  ({url})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._list.addItem(item)
        self._update_buttons()

    def _current_name(self):
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_buttons(self):
        name = self._current_name()
        has  = name is not None
        self._edit_btn.setEnabled(has)
        self._del_btn.setEnabled(has)
        self._con_btn.setEnabled(has)
        if name:
            signed = self._store.is_signed_in(name)
            self._con_btn.setText("Sign out" if signed else "Sign in…")

    def _add(self):
        dlg = ConnectionDialog(parent=self)
        if dlg.exec():
            self._store.save_connection(
                dlg.result_name, dlg.result_url,
                username = dlg.entered_username if dlg.save_credentials else "",
                password = dlg.entered_password if dlg.save_credentials else "",
            )
            if dlg.result_client:
                self._store.set_client(dlg.result_name, dlg.result_client)
            self._refresh()

    def _edit(self):
        name = self._current_name()
        if not name:
            return
        dlg = ConnectionDialog(
            name=name, url=self._store.connection_url(name), parent=self
        )
        if dlg.exec():
            self._store.save_connection(
                dlg.result_name, dlg.result_url,
                username = dlg.entered_username if dlg.save_credentials else "",
                password = dlg.entered_password if dlg.save_credentials else "",
                old_name = name,
            )
            if dlg.result_client:
                self._store.set_client(dlg.result_name, dlg.result_client)
            self._refresh()

    def _remove(self):
        name = self._current_name()
        if not name:
            return
        r = QMessageBox.question(
            self, "Remove", f"Remove '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._store.remove_connection(name)
            self._refresh()

    def _connect(self):
        name = self._current_name()
        if not name:
            return
        if self._store.is_signed_in(name):
            self._store.sign_out(name)
            self._refresh()
        else:
            client = self._store.ensure_client(name, parent=self)
            self._refresh()
