import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QDialog, QWidget

import main_interface
from autoscript_api import AutoScriptAPI
from user_management_dialog import UserEditorDialog, UserManagementDialog


USERS = [
    {
        "id": "admin-id",
        "username": "admin",
        "role": "admin",
        "is_active": True,
        "created_at": "2026-08-17T09:00:00Z",
    },
    {
        "id": "operator-id",
        "username": "nameer",
        "role": "operator",
        "is_active": True,
        "created_at": "2026-08-17T09:05:00Z",
    },
]


class RecordingAPI:
    def __init__(self):
        self.users = [dict(user) for user in USERS]
        self.created = []
        self.updated = []

    def list_users(self):
        return self.users

    def create_user(self, username, password, role):
        self.created.append((username, password, role))
        return {}

    def update_user(self, user_id, **changes):
        self.updated.append((user_id, changes))
        return {}


class DummyPage(QWidget):
    def refresh_experiments(self):
        pass

    def set_experiment(self, _experiment):
        pass


class UserManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_api_client_user_contract(self):
        api = AutoScriptAPI(base_url="https://example.invalid", token="token")
        with patch.object(api, "_json_request", return_value=[]) as request:
            self.assertEqual(api.list_users(), [])
            request.assert_called_once_with("GET", "/api/v1/users")

        with patch.object(api, "_json_request", return_value={}) as request:
            api.create_user("new-user", "long-password", "researcher")
            request.assert_called_once_with(
                "POST",
                "/api/v1/users",
                {
                    "username": "new-user",
                    "password": "long-password",
                    "role": "researcher",
                },
            )

        with patch.object(api, "_json_request", return_value={}) as request:
            api.update_user(
                "user-id", role="operator", password="replacement-password"
            )
            request.assert_called_once_with(
                "PATCH",
                "/api/v1/users/user-id",
                {"role": "operator", "password": "replacement-password"},
            )

        with self.assertRaises(ValueError):
            api.update_user("user-id", password_hash="forbidden")

    def test_dialog_lists_users_and_password_is_optional_for_edit(self):
        api = RecordingAPI()
        dialog = UserManagementDialog(
            api=api, current_user={"id": "admin-id", "role": "admin"}
        )
        try:
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.table.item(1, 0).text(), "nameer")
            self.assertEqual(dialog.table.item(1, 1).text(), "operator")
            editor = UserEditorDialog(dialog, user=USERS[1])
            try:
                values = editor.values()
                self.assertNotIn("password", values)
                self.assertEqual(values["username"], "nameer")
                self.assertEqual(values["role"], "operator")
            finally:
                editor.close()
        finally:
            dialog.close()

    def test_edit_sends_password_only_when_entered(self):
        api = RecordingAPI()
        dialog = UserManagementDialog(api=api, current_user=USERS[0])
        dialog.table.selectRow(1)

        class AcceptedEditor:
            def __init__(self, _parent, *, user):
                self.user = user

            def exec_(self):
                return QDialog.Accepted

            def values(self):
                return {
                    "username": "nameer",
                    "role": "operator",
                    "is_active": True,
                    "password": "new-secure-password",
                }

            def clear_passwords(self):
                pass

        try:
            with patch(
                "user_management_dialog.UserEditorDialog", AcceptedEditor
            ):
                dialog.edit_selected_user()
            self.assertEqual(
                api.updated,
                [
                    (
                        "operator-id",
                        {
                            "username": "nameer",
                            "role": "operator",
                            "is_active": True,
                            "password": "new-secure-password",
                        },
                    )
                ],
            )
        finally:
            dialog.close()

    def _main_window(self, role):
        manager = SimpleNamespace(load_catalog=MagicMock())
        with (
            patch.object(
                main_interface.MainInterface,
                "_authenticate_if_required",
                return_value={"id": f"{role}-id", "role": role},
            ),
            patch.object(main_interface, "MainMenu", DummyPage),
            patch.object(main_interface, "ExperimentResultsPage", DummyPage),
            patch.object(main_interface, "create_update_manager", return_value=manager),
            patch.object(main_interface.QTimer, "singleShot"),
        ):
            return main_interface.MainInterface()

    def test_user_management_menu_is_admin_only(self):
        admin = self._main_window("admin")
        researcher = self._main_window("researcher")
        try:
            admin_actions = {
                action.text()
                for action in admin.menuBar().actions()
            }
            researcher_actions = {
                action.text()
                for action in researcher.menuBar().actions()
            }
            self.assertIn("Administration", admin_actions)
            self.assertNotIn("Administration", researcher_actions)
            administration = next(
                action.menu()
                for action in admin.menuBar().actions()
                if action.text() == "Administration"
            )
            self.assertIn(
                "User Management…",
                {action.text() for action in administration.actions()},
            )
        finally:
            admin.builder_process_timer.stop()
            researcher.builder_process_timer.stop()
            admin.close()
            researcher.close()


if __name__ == "__main__":
    unittest.main()
