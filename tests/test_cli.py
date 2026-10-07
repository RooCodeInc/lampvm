"""Host safety contracts, isolated from Tart, the desktop, and real VM state.

These exercise CLI dispatch and artifact failure paths with a fake guest. They do
not substitute for the real-VM proof suite.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("lampvm_cli_tests", str(ROOT / "bin/lampvm"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class IsolatedState:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lampvm-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.environment = mock.patch.dict(cli.os.environ, {
            "LAMPVM_STATE_DIR": str(self.state), "LAMPVM_NAME": "private-test",
            "LAMPVM_CONFIG": str(self.state / "config.json"),
            "LAMPVM_TART": str(self.state / "fake-tart"), "LAMPVM_APP": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.state / "fake-tart").touch()
        (self.state / "config.json").write_text("{}\n")

    def vm(self, app=None):
        return cli.VM(argparse.Namespace(state_dir=None, config=None, name=None, app=app))

    def invoke(self, arguments, vm):
        with mock.patch.object(sys, "argv", ["lampvm", *arguments]), \
             mock.patch.object(cli, "VM", return_value=vm), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main()


class CLISafetyTests(IsolatedState, unittest.TestCase):
    def test_unowned_destroy_cannot_reach_stop_or_delete(self):
        vm = self.vm()
        with mock.patch.object(vm, "stop") as stop, mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "ownership record"):
                self.invoke(["destroy", "--yes"], vm)
        stop.assert_not_called()
        tart.assert_not_called()

    def test_destroy_deletes_only_selected_owned_vm(self):
        vm = self.vm()
        vm.marker().parent.mkdir()
        vm.marker().write_text(json.dumps({"name": vm.name}))
        with mock.patch.object(vm, "stop") as stop, mock.patch.object(vm, "t") as tart:
            self.invoke(["destroy", "--yes"], vm)
        stop.assert_called_once_with()
        tart.assert_called_once_with("delete", "private-test")
        self.assertFalse(vm.marker().exists())

    def test_clean_snapshot_cannot_be_destroyed(self):
        vm = self.vm()
        vm.marker().parent.mkdir()
        vm.marker().write_text(json.dumps({"name": vm.name, "role": "clean"}))
        with mock.patch.object(vm, "stop") as stop, mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "Clean snapshots"):
                self.invoke(["destroy", "--yes"], vm)
        stop.assert_not_called()
        tart.assert_not_called()
        self.assertTrue(vm.marker().exists())

    def test_running_clone_source_is_never_modified(self):
        vm = self.vm()
        machines = [{"Name": "other-session", "Source": "local", "Running": True}]
        with mock.patch.object(vm, "machines", return_value=machines), mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "stopped VM"):
                self.invoke(["clone", "private-copy", "--from", "other-session"], vm)
        tart.assert_not_called()
        self.assertFalse(vm.marker("private-copy").exists())

    def test_existing_clone_destination_is_never_replaced(self):
        vm = self.vm()
        with mock.patch.object(vm, "exists", return_value=True), mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "already exists"):
                self.invoke(["clone", "occupied"], vm)
        tart.assert_not_called()

    def test_nonfinite_slot_wait_is_rejected_before_boot(self):
        for value in ["nan", "inf", "-inf"]:
            with self.subTest(value=value):
                vm = self.vm()
                with mock.patch.object(vm, "start") as start:
                    with self.assertRaises((cli.Error, SystemExit)):
                        self.invoke(["start", "--wait=" + value], vm)
                start.assert_not_called()

    def test_raw_input_arity_is_checked_before_activating_any_app(self):
        for arguments in [["click", "1", "2", "3"], ["scroll", "1", "2", "3", "4", "5"], ["drag", "1", "2", "3"]]:
            with self.subTest(arguments=arguments):
                vm = self.vm(app="Configured")
                with mock.patch.object(vm, "need"), mock.patch.object(vm, "control") as control:
                    with self.assertRaises((cli.Error, SystemExit)):
                        self.invoke(arguments, vm)
                control.assert_not_called()

    def test_failed_operation_releases_its_exclusive_lock(self):
        path = self.state / "locks" / "private.lock"
        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            with cli.lock(path, timeout=0):
                raise RuntimeError("operation failed")
        # A leaked flock would fail this second acquisition immediately.
        with cli.lock(path, timeout=0):
            pass

    def test_exec_preserves_arguments_without_shell_expansion(self):
        vm = self.vm()
        dangerous = "$(touch /tmp/not-a-command); `whoami`"
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest") as guest:
            self.invoke(["exec", "/usr/bin/printf", "%s", dangerous], vm)
        guest.assert_called_once_with("/usr/bin/printf", "%s", dangerous)

    def test_type_double_dash_preserves_literal_option_text(self):
        vm = self.vm()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "control") as control:
            self.invoke(["type", "--", "--app"], vm)
        control.assert_called_once_with("type", "--app")

    def test_explicit_application_overrides_config_for_selector(self):
        (self.state / "config.json").write_text(json.dumps({"app": "Configured"}))
        args = cli.parser().parse_args(["find", "Search", "--app", "Chosen"])
        vm = cli.VM(args)
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "control") as control:
            self.invoke(["find", "Search", "--app", "Chosen"], vm)
        control.assert_called_once_with("find", "Chosen", "Search")

    def test_no_app_specific_default(self):
        vm = self.vm()
        with self.assertRaisesRegex(cli.Error, "Specify APP"):
            vm.application()

    def test_vm_name_cannot_escape_state_or_inject_cli_option(self):
        for name in ["../someone", "/tmp/vm", "--help", "x/y", "x\nother"]:
            with self.subTest(name=name), self.assertRaises(cli.Error):
                cli.valid_name(name)


class CaptureSafetyTests(IsolatedState, unittest.TestCase):
    def artifact_args(self, file=None):
        return argparse.Namespace(app=None, file=file, seconds=1)

    def fake_capture(self, *arguments, **kwargs):
        if arguments[0] == "/bin/cat":
            kwargs["stdout"].write(b"fake-image-bytes")

    def test_each_capture_uses_distinct_guest_and_host_files(self):
        vm = self.vm()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=self.fake_capture) as guest, \
             mock.patch.object(cli.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()):
            vm.artifact(self.artifact_args())
            vm.artifact(self.artifact_args())
        captures = [call.args[-1] for call in guest.call_args_list if call.args[0] == "/usr/sbin/screencapture"]
        self.assertEqual(len(set(captures)), 2)
        self.assertEqual(len(list((self.state / "artifacts").glob("*.png"))), 2)

    def test_partial_failed_copy_is_removed_and_guest_temp_is_cleaned(self):
        vm = self.vm()
        destination = self.state / "failed.png"
        def failing_copy(*arguments, **kwargs):
            if arguments[0] == "/bin/cat":
                kwargs["stdout"].write(b"partial")
                raise cli.Error("guest disconnected")
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=failing_copy), \
             mock.patch.object(cli.subprocess, "run") as cleanup:
            with self.assertRaisesRegex(cli.Error, "disconnected"):
                vm.artifact(self.artifact_args(str(destination)))
        self.assertFalse(destination.exists(), "failed captures must not leave a plausible artifact")
        self.assertEqual(cleanup.call_args.args[0][-3:-1], ["/bin/rm", "-f"])

    def test_capture_publication_race_preserves_other_calls_output(self):
        vm = self.vm()
        destination = self.state / "shared-request.png"
        def racing_copy(*arguments, **kwargs):
            if arguments[0] == "/bin/cat":
                kwargs["stdout"].write(b"our-image")
                destination.write_bytes(b"other-calls-image")
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=racing_copy), \
             mock.patch.object(cli.subprocess, "run"):
            with self.assertRaises(FileExistsError):
                vm.artifact(self.artifact_args(str(destination)))
        self.assertEqual(destination.read_bytes(), b"other-calls-image")
        self.assertFalse(list(self.state.glob("*.partial")))

    def test_existing_capture_output_is_not_overwritten(self):
        vm = self.vm()
        destination = self.state / "keep.png"
        destination.write_bytes(b"existing-proof")
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest") as guest:
            with self.assertRaisesRegex(cli.Error, "already exists"):
                vm.artifact(self.artifact_args(str(destination)))
        guest.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"existing-proof")


if __name__ == "__main__":
    unittest.main()
