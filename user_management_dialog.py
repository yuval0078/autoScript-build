"""Administrator-only user management for the AutoScript Interface."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from autoscript_api import APIError, AutoScriptAPI


ROLES = ("admin", "researcher", "operator")
ROLE_DESCRIPTIONS = {
    "admin": "Full access, including users, updates and maintenance.",
    "researcher": "All research features; no administration or maintenance.",
    "operator": "Run and analyze; cannot author experiments or delete results.",
}


class UserEditorDialog(QDialog):
    """Collect a new user or an update without retaining password text."""

    def __init__(self, parent=None, *, user=None):
        super().__init__(parent)
        self.user = dict(user or {})
        self.is_new = user is None
        self.setWindowTitle("Add User" if self.is_new else "Edit User")
        self.setMinimumWidth(470)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit(str(self.user.get("username", "")))
        self.username.setMaxLength(64)
        form.addRow("Username:", self.username)

        self.role = QComboBox()
        self.role.addItems(ROLES)
        current_role = str(self.user.get("role", "researcher"))
        self.role.setCurrentText(
            current_role if current_role in ROLES else "researcher"
        )
        form.addRow("Role:", self.role)

        self.role_help = QLabel()
        self.role_help.setWordWrap(True)
        self.role_help.setStyleSheet("color: #5f6b76;")
        form.addRow("", self.role_help)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setMaxLength(1024)
        self.password.setPlaceholderText(
            "At least 10 characters"
            if self.is_new
            else "Leave blank to keep the current password"
        )
        form.addRow("Password:", self.password)

        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.Password)
        self.confirm_password.setMaxLength(1024)
        form.addRow("Confirm password:", self.confirm_password)

        self.active = QCheckBox("Account is active")
        self.active.setChecked(bool(self.user.get("is_active", True)))
        self.active.setEnabled(not self.is_new)
        form.addRow("Status:", self.active)
        layout.addLayout(form)

        if not self.is_new:
            note = QLabel(
                "Changing a password or deactivating an account signs that user "
                "out on every computer."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #8a5a00;")
            layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.role.currentTextChanged.connect(self._update_role_help)
        self._update_role_help(self.role.currentText())

    def _update_role_help(self, role):
        self.role_help.setText(ROLE_DESCRIPTIONS.get(role, ""))

    def _validate_and_accept(self):
        username = self.username.text().strip()
        password = self.password.text()
        confirmation = self.confirm_password.text()
        if len(username) < 3:
            QMessageBox.warning(
                self, "Invalid username", "Username must contain at least 3 characters."
            )
            return
        if self.is_new and len(password) < 10:
            QMessageBox.warning(
                self, "Invalid password", "Password must contain at least 10 characters."
            )
            return
        if password and len(password) < 10:
            QMessageBox.warning(
                self, "Invalid password", "Password must contain at least 10 characters."
            )
            return
        if password != confirmation:
            QMessageBox.warning(
                self, "Passwords do not match", "Enter the same password in both fields."
            )
            return
        self.accept()

    def values(self):
        values = {
            "username": self.username.text().strip(),
            "role": self.role.currentText(),
            "is_active": self.active.isChecked(),
        }
        password = self.password.text()
        if password:
            values["password"] = password
        return values

    def clear_passwords(self):
        self.password.clear()
        self.confirm_password.clear()


class UserManagementDialog(QDialog):
    """List and administer application users through the protected API."""

    def __init__(self, parent=None, *, api=None, current_user=None):
        super().__init__(parent)
        self.api = api or AutoScriptAPI(timeout=20)
        self.current_user = dict(current_user or {})
        self.users = []
        self.setWindowTitle("AutoScript User Management")
        self.resize(760, 440)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Manage who can access AutoScript. Only administrators can open "
            "this screen or use its API operations."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Username", "Role", "Status", "Created"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.table.itemSelectionChanged.connect(self._sync_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_selected_user())
        layout.addWidget(self.table)

        controls = QHBoxLayout()
        self.status = QLabel("")
        controls.addWidget(self.status, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_users)
        controls.addWidget(self.refresh_button)
        self.add_button = QPushButton("Add user…")
        self.add_button.clicked.connect(self.add_user)
        controls.addWidget(self.add_button)
        self.edit_button = QPushButton("Edit selected…")
        self.edit_button.clicked.connect(self.edit_selected_user)
        controls.addWidget(self.edit_button)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self._sync_actions()
        self.refresh_users()

    def _selected_user(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.users):
            return None
        return self.users[row]

    def _sync_actions(self):
        self.edit_button.setEnabled(self._selected_user() is not None)

    def refresh_users(self):
        self.refresh_button.setEnabled(False)
        self.status.setText("Loading users…")
        try:
            self.users = list(self.api.list_users() or [])
        except APIError as exc:
            self.status.setText("Could not load users")
            QMessageBox.critical(self, "User Management Failed", str(exc))
            return
        finally:
            self.refresh_button.setEnabled(True)

        self.table.setRowCount(len(self.users))
        for row, user in enumerate(self.users):
            created_at = str(user.get("created_at", ""))
            if "T" in created_at:
                created_at = created_at.replace("T", " ").split(".", 1)[0].rstrip("Z")
            values = (
                str(user.get("username", "")),
                str(user.get("role", "")),
                "Active" if user.get("is_active") else "Inactive",
                created_at or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, str(user.get("id", "")))
                self.table.setItem(row, column, item)
        self.table.clearSelection()
        self.status.setText(f"{len(self.users)} user(s)")
        self._sync_actions()

    def add_user(self):
        editor = UserEditorDialog(self)
        try:
            if editor.exec_() != QDialog.Accepted:
                return
            values = editor.values()
            self.api.create_user(
                values["username"], values["password"], values["role"]
            )
            self.status.setText(f"Created {values['username']}")
        except APIError as exc:
            QMessageBox.critical(self, "Could Not Create User", str(exc))
            return
        finally:
            editor.clear_passwords()
        self.refresh_users()

    def edit_selected_user(self):
        user = self._selected_user()
        if user is None:
            return
        editor = UserEditorDialog(self, user=user)
        try:
            if editor.exec_() != QDialog.Accepted:
                return
            values = editor.values()
            changes = {
                "username": values["username"],
                "role": values["role"],
                "is_active": values["is_active"],
            }
            if "password" in values:
                changes["password"] = values["password"]
            self.api.update_user(user["id"], **changes)
            self.status.setText(f"Updated {values['username']}")
        except APIError as exc:
            QMessageBox.critical(self, "Could Not Update User", str(exc))
            return
        finally:
            editor.clear_passwords()
        self.refresh_users()


__all__ = [
    "ROLES",
    "ROLE_DESCRIPTIONS",
    "UserEditorDialog",
    "UserManagementDialog",
]
