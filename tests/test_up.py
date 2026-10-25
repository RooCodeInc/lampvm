"""`up` and the image it prepares, without Tart, a VM or the network."""
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
loader = importlib.machinery.SourceFileLoader("lampvm_up_tests", str(ROOT / "bin/lampvm"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(cli)
setup_spec = importlib.util.spec_from_file_location("lampvm_setup_up", ROOT / "lib/setup.py")
setup = importlib.util.module_from_spec(setup_spec)
setup_spec.loader.exec_module(setup)

IMAGE = "ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:" + "0" * 64


class Up(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lampvm-up-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        environment = mock.patch.dict(cli.os.environ, {"LAMPVM_STATE_DIR": str(self.dir / "state"), "LAMPVM_NAME": "private-up",
                                                       "LAMPVM_TART": str(self.dir / "fake-tart"), "LAMPVM_CONFIG": str(self.dir / "config.json"),
                                                       "LAMPVM_SESSION": ""})
        environment.start()
        self.addCleanup(environment.stop)
        (self.dir / "config.json").write_text("{}\n")
        (self.dir / "fake-tart").touch()
        self.spec = self.dir / "vm.json"
        self.spec.write_text(json.dumps({"image": {"name": "xcode-image", "from": IMAGE},
                                         "ready": [{"exec": ["/usr/bin/xcodebuild", "-version"], "expect": "Xcode 27", "timeout": 1}]}))

    def up(self, guest_output, returncode=0):
        vm = cli.VM(argparse.Namespace(state_dir=None, config=None, name=None, app=None))
        steps = []
        vm.exists = lambda name: False
        vm.clone = lambda source, target: steps.append(("clone", source, target))
        vm.start = lambda *a: steps.append(("start",))
        answer = mock.Mock(returncode=returncode, stdout=guest_output)
        out = io.StringIO()
        with mock.patch.object(cli, "prepare_image", lambda vm, image, wait: steps.append(("image", image["name"]))), \
             mock.patch.object(cli.subprocess, "run", return_value=answer), mock.patch.object(cli.time, "sleep"), \
             mock.patch.object(sys, "argv", ["lampvm", "up", str(self.spec)]), mock.patch.object(cli, "VM", return_value=vm), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.main()
            finally:
                self.steps, self.out = steps, out.getvalue()

    def test_returns_ready_only_after_the_spec_check_passes(self):
        self.up("Xcode 27.0\nBuild version 27A266a\n")
        self.assertEqual(self.steps, [("image", "xcode-image"), ("clone", "xcode-image", "private-up"), ("start",)])
        self.assertTrue(self.out.rstrip().endswith("ready: private-up"), self.out)

    def test_a_failing_check_is_not_ready(self):
        with self.assertRaisesRegex(cli.Error, "Not ready: /usr/bin/xcodebuild -version gave exit 1"):
            self.up("xcode-select: error: tool 'xcodebuild' requires Xcode", returncode=1)
        self.assertNotIn("ready: private-up", self.out)

    def test_the_wrong_version_is_not_ready(self):
        with self.assertRaisesRegex(cli.Error, "without 'Xcode 27'"):
            self.up("Xcode 26.4\n")


class ImageSetup(unittest.TestCase):
    """Preparing a spec's image must not change the default clean image other sessions clone."""

    def prepare(self, *extra):
        with tempfile.TemporaryDirectory(prefix="lampvm-image-") as temporary:
            state = Path(temporary)
            runtime = state / "config.json"
            runtime.write_text(json.dumps({"vm": "lampvm", "clean": "lampvm-clean", "image": IMAGE}) + "\n")
            (state / "bin").mkdir()
            (state / "bin/vmctl").touch()
            vm = mock.MagicMock()
            vm.exists.return_value = False
            vm.running.return_value = False
            vm.start.side_effect = RuntimeError("stopped before boot")
            argv = ["setup.py", "--state-dir", str(state), "--name", "img-build", "--clean", "img", "--no-build", "--no-download", "--image", IMAGE, *extra]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(setup.platform, "system", return_value="Darwin"), \
                 mock.patch.object(setup.platform, "machine", return_value="arm64"), \
                 mock.patch.object(setup, "capture", side_effect=lambda a, **k: "26.0" if "sw_vers" in str(a[0]) else setup.VERSION), \
                 mock.patch.object(setup.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 mock.patch.object(setup, "tart_binary", return_value="tart"), mock.patch.object(setup, "VM", return_value=vm), \
                 mock.patch.dict(setup.os.environ):
                setup.os.environ.pop("LAMPVM_CONFIG", None)
                with self.assertRaisesRegex(RuntimeError, "stopped before boot"):
                    setup.main()
            clone = [c.args for c in vm.command.call_args_list if c.args[:1] == ("clone",)]
            return json.loads(runtime.read_text()), clone

    def test_keep_config_leaves_the_default_clean_image(self):
        config, clone = self.prepare("--keep-config")
        self.assertEqual(config["clean"], "lampvm-clean")
        self.assertEqual(clone, [("clone", IMAGE, "img-build")])

    def test_without_keep_config_setup_records_the_new_image_as_default(self):
        config, _ = self.prepare()
        self.assertEqual(config["clean"], "img")


if __name__ == "__main__":
    unittest.main()
