"""Safety checks that never boot a VM or reach the network."""
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("lampvm_setup", Path(__file__).resolve().parents[1] / "lib/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupSafety(unittest.TestCase):
    def test_resume_preserves_guest_hardware_identity(self):
        vm = MagicMock(name="vm")
        vm.name = "owned"
        config = dict(cpu=4, memory=8192, display="1440x900")
        setup.configure_vm(vm, config, fresh=True)
        self.assertIn("--random-serial", vm.command.call_args.args)
        setup.configure_vm(vm, config, fresh=False)
        self.assertNotIn("--random-serial", vm.command.call_args.args)
        self.assertNotIn("--random-mac", vm.command.call_args.args)

    def test_guest_panic_prevents_clean_snapshot(self):
        vm = MagicMock()
        vm.text.return_value = "/Library/Logs/DiagnosticReports/panic-full-test.panic"
        with self.assertRaisesRegex(RuntimeError, "No clean snapshot"):
            setup.check_guest_health(vm, "/guest/vmctl")
        vm.command.assert_not_called()

    def test_inherited_panic_report_is_preserved_without_false_failure(self):
        vm = MagicMock()
        report = "/Library/Logs/DiagnosticReports/panic-full-historical.panic"
        vm.text.return_value = report
        with patch.object(setup.subprocess, "run", return_value=MagicMock(stdout="")):
            setup.check_guest_health(vm, "/guest/vmctl", {report})
        vm.command.assert_not_called()

    def test_visible_restart_report_is_not_dismissed(self):
        vm = MagicMock()
        vm.text.return_value = ""
        with patch.object(setup.subprocess, "run", return_value=MagicMock(stdout="Your computer was restarted because of a problem")):
            with self.assertRaisesRegex(RuntimeError, "No clean snapshot"):
                setup.check_guest_health(vm, "/guest/vmctl")
        vm.command.assert_not_called()

    def test_config_overlay_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime.json"
            source = Path(temporary) / "lamp.json"
            runtime.write_text('{"vm":"private","cpu":4,"app":"Old"}')
            source.write_text('{"app":"Lamp","wipe":{"paths":["Library/Fake"]}}\n')
            before = source.read_bytes()
            merged = setup.load_config(runtime, source)
            self.assertEqual(merged["vm"], "private")
            self.assertEqual(merged["app"], "Lamp")
            setup.atomic_json(runtime, merged)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(json.loads(runtime.read_text()), merged)

    def test_missing_explicit_config_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                setup.load_config(Path(temporary) / "runtime.json", Path(temporary) / "missing.json")

    def test_unowned_or_unrelated_clean_collision_is_refused(self):
        vm = MagicMock()
        vm.exists.return_value = True
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker.json"
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                setup.check_owned(vm, "private", marker)
            marker.write_text('{"name":"clean","role":"clean","source":"other-working"}')
            with self.assertRaisesRegex(RuntimeError, "does not belong"):
                setup.check_owned(vm, "clean", marker, clean_source="private")
        vm.command.assert_not_called()
        vm.start.assert_not_called()
        vm.stop.assert_not_called()

    def test_name_restrictions(self):
        for name in ("", ".hidden", "../other", "has spaces", "x" * 81, None):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                setup.valid_name(name)
        self.assertEqual(setup.valid_name("private-1._"), "private-1._")

    def test_occupied_slots_never_boot_or_stop_other_vms(self):
        vm = setup.VM("/never/run/tart", "owned")
        with patch.object(vm, "all", return_value=[{"Name": "other-a", "Running": True}, {"Name": "other-b", "Running": True}]), patch.object(setup, "lock", return_value=contextlib.nullcontext()), patch.object(setup.subprocess, "run", return_value=MagicMock(stdout="")), patch.object(setup.subprocess, "Popen") as boot, patch.object(vm, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "No VM was stopped"):
                vm.start(0)
            boot.assert_not_called()
            command.assert_not_called()

    def test_invalid_resources_refused_before_tart_changes(self):
        defaults = dict(cpu=4, memory=8192, display="1440x900", image=setup.IMAGE)
        invalid = [dict(cpu=0), dict(cpu=True), dict(memory=-1), dict(memory="8192"), dict(display="0x900"), dict(display="1440x0"), dict(display="1440x900; rm -rf /"), dict(image="../../foreign"), dict(image="ghcr.io/base@sha256:invalid")]
        with patch.object(setup, "run") as mutation, patch.object(setup.os, "cpu_count", return_value=8):
            for override in invalid:
                with self.subTest(override=override), self.assertRaises(RuntimeError):
                    setup.validate_config(dict(defaults, **override))
            mutation.assert_not_called()

    def test_running_source_is_refused_without_modifying_any_vm(self):
        vm = MagicMock()
        vm.exists.side_effect = lambda name: name == "foreign-running"
        vm.running.side_effect = lambda name: name == "foreign-running"
        with tempfile.TemporaryDirectory() as temporary, patch.object(setup, "STATE", Path(temporary)), patch.object(setup.sys, "argv", ["setup.py", "--name", "private", "--from", "foreign-running"]), patch.dict(setup.os.environ, {}, clear=True), patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup.platform, "machine", return_value="arm64"), patch.object(setup.os, "cpu_count", return_value=8), patch.object(setup, "capture", side_effect=["27.0", "2.32.1"]), patch.object(setup.subprocess, "run", return_value=MagicMock(returncode=0)), patch.object(setup, "run"), patch.object(setup, "tart_binary", return_value=Path("tart")), patch.object(setup, "VM", return_value=vm):
            with self.assertRaisesRegex(RuntimeError, "existing stopped local VM"):
                setup.main()
        vm.command.assert_not_called()
        vm.start.assert_not_called()
        vm.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
