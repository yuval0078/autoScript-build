import os
import unittest
from pathlib import Path
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

    def test_api_client_device_token_contract(self):
        api = AutoScriptAPI(base_url="https://example.invalid", token="token")
        with patch.object(api, "_json_request", return_value={}) as request:
            api.create_device_token("Current device", "current-password")
            request.assert_called_once_with(
                "POST",
                "/api/v1/auth/device-tokens",
                {"label": "Current device", "password": "current-password"},
            )
        with patch.object(api, "_json_request", return_value=[]) as request:
            self.assertEqual(api.list_device_tokens(), [])
            request.assert_called_once_with("GET", "/api/v1/auth/device-tokens")
        with patch.object(api, "_json_request", return_value=None) as request:
            api.revoke_device_token("token-id")
            request.assert_called_once_with(
                "DELETE", "/api/v1/auth/device-tokens/token-id"
            )

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
            self.assertIn("Updates", admin_actions)
            self.assertIn("Updates", researcher_actions)
            administration = next(
                action.menu()
                for action in admin.menuBar().actions()
                if action.text() == "Administration"
            )
            self.assertIn(
                "User Management…",
                {action.text() for action in administration.actions()},
            )
            self.assertNotIn("Research Data", admin_actions)
            self.assertNotIn("Research Data", researcher_actions)
        finally:
            admin.builder_process_timer.stop()
            researcher.builder_process_timer.stop()
            admin.close()
            researcher.close()

    def test_researcher_can_open_component_updates(self):
        window = self._main_window("researcher")
        dialogs = []

        class UpdatesDialog:
            def __init__(self, parent, *, manager, coordinator):
                self.parent = parent
                self.manager = manager
                self.coordinator = coordinator
                self.component_installed = MagicMock()
                self.interface_update_started = MagicMock()
                self.executed = False
                dialogs.append(self)

            def exec_(self):
                self.executed = True

        try:
            with patch.object(
                main_interface, "ComponentUpdatesDialog", UpdatesDialog
            ):
                window.show_component_updates()
            dialog = dialogs[0]
            self.assertTrue(dialog.executed)
            self.assertIs(dialog.parent, window)
            self.assertIs(dialog.manager, window.component_manager)
        finally:
            window.builder_process_timer.stop()
            window.close()

    def test_operator_can_open_component_updates(self):
        window = self._main_window("operator")
        try:
            self.assertIn("Updates", {a.text() for a in window.menuBar().actions()})
            with patch.object(main_interface, "ComponentUpdatesDialog") as dialog:
                window.show_component_updates()
                dialog.return_value.exec_.assert_called_once_with()
            self.assertNotIn("Administration", {a.text() for a in window.menuBar().actions()})
        finally:
            window.builder_process_timer.stop()
            window.close()

    def test_interface_update_restart_launches_active_interface_and_quits(self):
        window = self._main_window("researcher")
        application = MagicMock()
        try:
            with (
                patch.object(
                    main_interface,
                    "component_launch_command",
                    return_value=["AutoScriptInterface-0.2.exe"],
                ) as resolve,
                patch.object(main_interface.subprocess, "Popen") as popen,
                patch.object(
                    main_interface.QApplication,
                    "instance",
                    return_value=application,
                ),
                patch.object(
                    main_interface,
                    "get_session_token",
                    return_value="memory-token",
                ),
            ):
                window._restart_after_interface_update()
            self.assertEqual(resolve.call_args.args[0], "interface")
            self.assertIs(resolve.call_args.kwargs["manager"], window.component_manager)
            popen.assert_called_once()
            self.assertEqual(
                popen.call_args.kwargs["env"]["AUTOSCRIPT_API_TOKEN"],
                "memory-token",
            )
            application.quit.assert_called_once_with()
        finally:
            window.builder_process_timer.stop()
            window.close()





if __name__ == "__main__":
    unittest.main()
