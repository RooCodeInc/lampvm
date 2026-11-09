"""An accounts VM holds real sign-ins: nothing copies it, and only the session it was handed to uses it."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from test_cli import IsolatedState, cli


class AccountsVMTests(IsolatedState, unittest.TestCase):
    def accounts_vm(self, grant=None):
        vm = self.vm()
        vm.marker().parent.mkdir(exist_ok=True)
        record = {"name": vm.name, "clean": "lampvm-clean", "role": "accounts", "phase": "ready"}
        if grant:
            record["grant"] = {"session": grant, "granted_at": 0, "boot": "{ sec = 100 }"}
        vm.marker().write_text(json.dumps(record))
        return vm

    def test_nothing_is_cloned_from_an_accounts_vm(self):
        vm = self.accounts_vm()
        machines = [{"Name": vm.name, "Source": "local", "Running": False}]
        with mock.patch.object(vm, "machines", return_value=machines), mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "accounts VM"):
                self.invoke(["clone", "private-copy", "--from", vm.name], vm)
        tart.assert_not_called()
        self.assertFalse(vm.marker("private-copy").exists())

    def test_reset_and_destroy_never_touch_an_accounts_vm(self):
        for command in (["reset", "--yes"], ["destroy", "--yes"]):
            with self.subTest(command=command[0]):
                vm = self.accounts_vm()
                with mock.patch.object(vm, "stop") as stop, mock.patch.object(vm, "t") as tart, \
                     mock.patch.object(vm, "reset") as reset:
                    with self.assertRaisesRegex(cli.Error, "accounts VM"):
                        self.invoke(command, vm)
                stop.assert_not_called(); tart.assert_not_called(); reset.assert_not_called()
                self.assertTrue(vm.marker().exists())

    def test_only_the_granted_session_reaches_the_guest(self):
        for session, grant, allowed in [("t_agent", None, False), ("t_other", "t_agent", False),
                                        ("", "t_agent", False), ("t_agent", "t_agent", True)]:
            with self.subTest(session=session, grant=grant):
                vm = self.accounts_vm(grant)
                with mock.patch.dict(cli.os.environ, {"LAMPVM_SESSION": session}), \
                     mock.patch.object(vm, "running", return_value=True), \
                     mock.patch.object(vm, "boot_time", return_value="{ sec = 100 }"), \
                     mock.patch.object(vm, "watched") as watched:
                    if allowed:
                        self.invoke(["exec", "/usr/bin/true"], vm)
                    else:
                        with self.assertRaisesRegex(cli.Error, "accounts VM"):
                            self.invoke(["exec", "/usr/bin/true"], vm)
                self.assertEqual(watched.called, allowed)

    def test_a_restart_inside_the_guest_ends_the_hand_over(self):
        vm = self.accounts_vm("t_agent")
        with mock.patch.dict(cli.os.environ, {"LAMPVM_SESSION": "t_agent"}), \
             mock.patch.object(vm, "running", return_value=True), \
             mock.patch.object(vm, "boot_time", return_value="{ sec = 200 }"), \
             mock.patch.object(vm, "watched") as watched:
            with self.assertRaisesRegex(cli.Error, "restarted"):
                self.invoke(["exec", "/usr/bin/true"], vm)
        watched.assert_not_called()
        self.assertNotIn("grant", json.loads(vm.marker().read_text()))

    def test_reset_never_copies_an_accounts_vm_into_another_vm(self):
        accounts = self.accounts_vm()
        vm = cli.VM(cli.argparse.Namespace(state_dir=None, config=None, name="other-vm", app=None))
        vm.marker().write_text(json.dumps({"name": "other-vm", "clean": accounts.name}))
        machines = [{"Name": accounts.name, "Source": "local", "Running": False}]
        with mock.patch.object(vm, "machines", return_value=machines), mock.patch.object(vm, "reset") as reset, \
             mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "accounts VM"):
                self.invoke(["reset", "--yes"], vm)
        reset.assert_not_called(); tart.assert_not_called()

    def test_revoke_does_not_wait_for_a_granted_command_holding_the_lock(self):
        vm = self.accounts_vm("t_agent")
        with cli.locks(vm.state, [vm.name]), mock.patch.object(cli, "lock", side_effect=AssertionError("revoke waited for a lock")), \
             mock.patch.object(vm, "stop") as stop:
            self.invoke(["accounts", "revoke", vm.name], vm)
        stop.assert_called_once_with()
        self.assertNotIn("grant", json.loads(vm.marker().read_text()))

    def test_stopping_or_booting_ends_the_hand_over(self):
        vm = self.accounts_vm("t_agent")
        with mock.patch.object(vm, "running", return_value=False):
            self.invoke(["stop"], vm)
        self.assertNotIn("grant", json.loads(vm.marker().read_text()))
        # A VM stopped outside lampvm keeps its record; the next boot ends the grant before anyone signs in.
        vm = self.accounts_vm("t_agent")
        with mock.patch.object(vm, "running", return_value=True), mock.patch.object(vm, "login_window", return_value=True), \
             mock.patch.object(vm, "unlocked", return_value=False):
            vm.boot()
        self.assertNotIn("grant", json.loads(vm.marker().read_text()))

    def test_person_commands_refuse_an_agent_without_a_terminal(self):
        vm = self.accounts_vm()
        for command in (["accounts", "create", "other-accounts"], ["accounts", "grant", vm.name, "--session", "t_agent"],
                        ["accounts", "destroy", vm.name, "--yes"]):
            with self.subTest(command=command[1]):
                with mock.patch.object(vm, "t") as tart, mock.patch.object(cli.getpass, "getpass") as asked:
                    with self.assertRaisesRegex(cli.Error, "at a terminal"):
                        self.invoke(command, vm)
                tart.assert_not_called(); asked.assert_not_called()
        self.assertNotIn("grant", json.loads(vm.marker().read_text()))

    def test_setup_never_prepares_or_images_an_accounts_vm(self):
        spec = importlib.util.spec_from_file_location("lampvm_setup_accounts", Path(__file__).resolve().parents[1] / "lib/setup.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        vm = self.accounts_vm()
        tart = mock.MagicMock()
        tart.exists.return_value = True
        with self.assertRaisesRegex(RuntimeError, "accounts VM"):
            setup.check_owned(tart, vm.name, vm.marker())


if __name__ == "__main__":
    unittest.main()
