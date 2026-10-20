"""Host safety contracts, isolated from Tart, the desktop, and real VM state.

These exercise CLI dispatch and artifact failure paths with a fake guest. They do
not substitute for the real-VM proof suite.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import re
import subprocess
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
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "watched") as watched:
            self.invoke(["exec", "/usr/bin/printf", "%s", dangerous], vm)
        watched.assert_called_once_with("exec", vm.name, "/usr/bin/printf", "%s", dangerous)

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

    def test_config_flag_choosing_the_desktop_outranks_the_app_environment(self):
        # Seen in a guest: LAMPVM_APP cropped a `--config {"app": null}` recording to one window.
        desktop, other = self.state / "desktop.json", self.state / "other.json"
        desktop.write_text('{"app": null}\n')
        other.write_text('{"app": "Chosen"}\n')
        with mock.patch.dict(cli.os.environ, {"LAMPVM_APP": "FromEnvironment"}):
            app = lambda *arguments: cli.VM(cli.parser().parse_args([*arguments, "record"])).app
            self.assertIsNone(app("--config", str(desktop)))
            self.assertEqual(app("--config", str(other)), "Chosen")
            self.assertEqual(app(), "FromEnvironment")
            (self.state / "config.json").write_text('{"app": null}\n')
            self.assertEqual(app(), "FromEnvironment")

    def test_openrouter_sends_only_its_key_on_stdin_and_the_configured_model(self):
        keys = self.state / "keys.json"
        keys.write_text(json.dumps({"openrouter": "sk-or-test-secret", "anthropic": "sk-ant-other"}))
        config = json.loads((ROOT / "config/lamp.json").read_text())
        config["openrouter"]["keys"] = str(keys)
        (self.state / "config.json").write_text(json.dumps(config))
        vm = self.vm()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "control") as control, mock.patch.object(vm, "t") as tart:
            self.invoke(["openrouter"], vm)
        control.assert_called_once_with("quit", "Lamp Dev")
        argv, options = tart.call_args.args, tart.call_args.kwargs
        self.assertEqual(argv[-3:], ("Lamp Dev", "dev.lamp.host.dev", "anthropic/claude-sonnet-5.5"))
        self.assertNotIn("sk-or-test-secret", " ".join(argv))
        self.assertEqual(json.loads(options["input"]), {"openrouter": "sk-or-test-secret"})

    def test_openrouter_without_a_key_pushes_nothing(self):
        config = json.loads((ROOT / "config/lamp.json").read_text())
        config["openrouter"]["keys"] = str(self.state / "missing.json")
        (self.state / "config.json").write_text(json.dumps(config))
        vm = self.vm()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "control") as control, mock.patch.object(vm, "t") as tart:
            with self.assertRaisesRegex(cli.Error, "No openrouter key"):
                self.invoke(["openrouter"], vm)
        control.assert_not_called()
        tart.assert_not_called()

    def test_no_app_specific_default(self):
        vm = self.vm()
        with self.assertRaisesRegex(cli.Error, "Specify APP"):
            vm.application()

    def test_vm_name_cannot_escape_state_or_inject_cli_option(self):
        for name in ["../someone", "/tmp/vm", "--help", "x/y", "x\nother"]:
            with self.subTest(name=name), self.assertRaises(cli.Error):
                cli.valid_name(name)


class ViewTests(IsolatedState, unittest.TestCase):
    def own(self, vm, *names):
        vm.marker().parent.mkdir(exist_ok=True)
        for name in names:
            vm.marker(name).write_text(json.dumps({"name": name}))

    def view(self, vm, arguments, running, port=True):
        machines = [{"Name": name, "Source": "local", "Running": True} for name in running]
        with mock.patch.object(vm, "machines", return_value=machines), mock.patch.object(vm, "tout", return_value="192.168.64.9"), \
             mock.patch.object(cli, "reachable", return_value=port), mock.patch.object(cli, "run") as opened:
            try:
                self.invoke(["view", *arguments], vm)
            finally:
                self.opened = opened

    def test_view_opens_selected_vm_without_touching_slots_or_action_lock(self):
        vm = self.vm()
        self.own(vm, "private-test")
        # A held action lock stands for the agent's command in progress.
        with cli.lock(self.state / "locks" / "private-test.lock", timeout=0):
            self.view(vm, [], ["private-test"])
        self.opened.assert_called_once_with(["/usr/bin/open", "vnc://admin:admin@192.168.64.9"])
        self.assertFalse((self.state / "slots.json").exists(), "watching must not count as agent activity")

    def test_view_without_a_name_shows_the_only_running_vm(self):
        vm = self.vm()
        self.own(vm, "agent-vm", "stopped-vm")
        with mock.patch.dict(cli.os.environ, {"LAMPVM_NAME": ""}):
            self.view(vm, [], ["agent-vm", "not-lampvm"])
        self.assertEqual(vm.name, "agent-vm")
        self.opened.assert_called_once()

    def test_view_lists_choices_when_two_are_running(self):
        vm = self.vm()
        self.own(vm, "agent-a", "agent-b")
        with mock.patch.dict(cli.os.environ, {"LAMPVM_NAME": ""}):
            with self.assertRaisesRegex(cli.Error, "lampvm view agent-a\n  lampvm view agent-b"):
                self.view(vm, [], ["agent-a", "agent-b"])
            self.view(vm, ["agent-b"], ["agent-a", "agent-b"])
        self.assertEqual(vm.name, "agent-b")

    def test_view_opens_nothing_when_stopped_unowned_or_sharing_is_off(self):
        vm = self.vm()
        self.own(vm, "private-test")
        for arguments, running, port, message in [([], [], True, "isn't running"), (["stranger"], ["stranger"], True, "ownership record"),
                                                  ([], ["private-test"], False, "Screen Sharing is off")]:
            with self.subTest(message=message), self.assertRaisesRegex(cli.Error, message):
                self.view(vm, arguments, running, port)
            self.opened.assert_not_called()
        with mock.patch.dict(cli.os.environ, {"LAMPVM_NAME": ""}), self.assertRaisesRegex(cli.Error, "No VM is running"):
            self.view(vm, [], [])


class CaptureSafetyTests(IsolatedState, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # These check file handling; the notification banner check reads the guest and is proven in a real VM.
        refuse = mock.patch.object(cli.VM, "refuse_banners")
        refuse.start()
        self.addCleanup(refuse.stop)

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

    def test_pointer_glides_only_while_a_recording_is_running_or_when_asked(self):
        vm = self.vm()
        seen = []
        def capture(*arguments, **kwargs):
            if arguments[0] == "/usr/sbin/screencapture":
                seen.append(vm.glide(argparse.Namespace(glide=False)))
            self.fake_capture(*arguments, **kwargs)
        self.assertIsNone(vm.glide(argparse.Namespace(glide=False)))
        self.assertEqual(vm.glide(argparse.Namespace(glide=True)), "asked")
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=capture), mock.patch.object(vm, "helper_version", return_value=1), \
             mock.patch.object(cli.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()):
            vm.artifact(self.artifact_args(), True)
            vm.artifact(self.artifact_args())
        self.assertEqual(seen, ["recording", None])
        self.assertIsNone(vm.glide(argparse.Namespace(glide=False)))

    def test_glide_is_decided_in_the_guest_by_helper_version(self):
        # Run the guest-side script against stand-in helpers: one from before `version`, one current.
        home = self.state / "home"
        (home / ".local/bin").mkdir(parents=True)
        helper = home / ".local/bin/vmctl"
        def run(why):
            return subprocess.run(["/bin/zsh", "-c", cli.GLIDING, "vmctl", why, "move", "1", "2"], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        helper.write_text('#!/bin/zsh\n[[ $1 == version ]] && { print -u2 "usage: vmctl ..."; exit 1 }\nprint -r -- "$*"\n'); helper.chmod(0o755)
        self.assertEqual((run("recording").returncode, run("recording").stdout), (0, "move 1 2\n"))
        self.assertEqual(run("asked").returncode, 2)
        self.assertIn("older than --glide", run("asked").stderr)
        helper.write_text('#!/bin/zsh\n[[ $1 == version ]] && { print 2; exit 0 }\nprint -r -- "$*"\n')
        self.assertEqual(run("recording").stdout, "move 1 2 --glide\n")
        self.assertEqual(run("asked").stdout, "move 1 2 --glide\n")

    def test_recording_uses_the_helper_when_it_can_record_and_screencapture_when_not(self):
        vm = self.vm()
        vm.ctl = "/Users/admin/.local/bin/vmctl"
        for version, expected in [(3, [vm.ctl, "record", "REMOTE", "--seconds", "1"]), (1, ["/usr/sbin/screencapture", "-x", "-v", "-V1", "-k", "REMOTE"])]:
            def capture(*arguments, **kwargs):
                self.fake_capture(*arguments, **kwargs)
                return mock.Mock(stdout='{"file":"x","frames":3,"seconds":0.75}')
            with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=capture) as guest, \
                 mock.patch.object(vm, "helper_version", return_value=version), mock.patch.object(cli.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()):
                vm.artifact(self.artifact_args(), True)
            sent = [re.sub(r"/tmp/lampvm-[0-9a-f]+\.mov", "REMOTE", part) for part in guest.call_args_list[0].args]
            self.assertEqual(sent, expected)

    def test_stop_interrupts_only_lampvm_recordings_and_says_when_it_cannot(self):
        vm = self.vm()
        vm.ctl = "/Users/admin/.local/bin/vmctl"
        self.assertTrue(re.search(cli.RECORDER, "/Users/admin/.local/bin/vmctl record /tmp/lampvm-0123abcd.mov --seconds 300"))
        self.assertFalse(re.search(cli.RECORDER, "/Users/admin/.local/bin/vmctl record /Users/admin/own.mov"))
        self.assertFalse(re.search(cli.RECORDER, "/usr/sbin/screencapture -x -v -V300 -k /tmp/lampvm-0123abcd.mov"))
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "helper_version", return_value=3), contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            vm.signal_recording("stop")
        self.assertEqual(run.call_args.args[0][1:], ["exec", vm.name, "/usr/bin/pkill", "-INT", "-f", cli.RECORDER])
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "helper_version", return_value=3), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(cli.Error, "No recording is running"):
                vm.signal_recording("stop")
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "helper_version", return_value=2), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(cli.Error, "cannot end a recording early"):
                vm.signal_recording("stop")

    def test_mark_reaches_only_a_helper_that_keeps_marks_and_lands_beside_the_movie(self):
        # Seen in a real guest on 2026-10-20: helper 6 reports marks, and the record call wrote them to take.timeline.json.
        vm = self.vm()
        vm.ctl = "/Users/admin/.local/bin/vmctl"
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "recordings", return_value=[]), mock.patch.object(vm, "control") as control:
            with self.assertRaisesRegex(cli.Error, "No recording is running"):
                vm.mark_recording("first mark")
        control.assert_not_called()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "recordings", return_value=[{"vm": vm.name}]), \
             mock.patch.object(vm, "helper_version", return_value=5), mock.patch.object(vm, "control") as control:
            with self.assertRaisesRegex(cli.Error, "cannot mark"):
                vm.mark_recording("first mark")
        control.assert_not_called()
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "recordings", return_value=[{"vm": vm.name}]), \
             mock.patch.object(vm, "helper_version", return_value=6), mock.patch.object(vm, "control") as control:
            vm.mark_recording("  first\nmark ")
        control.assert_called_once_with("mark", "first mark")
        movie = self.state / "take.mov"
        def capture(*arguments, **kwargs):
            self.fake_capture(*arguments, **kwargs)
            return mock.Mock(stdout='{"file":"x","frames":3,"seconds":8.44,"pausedSeconds":0,"marks":[{"seconds":3.07,"text":"first mark"},{"seconds":6.11,"text":"second mark"}]}')
        with mock.patch.object(vm, "need"), mock.patch.object(vm, "guest", side_effect=capture), mock.patch.object(vm, "helper_version", return_value=6), \
             mock.patch.object(cli.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()) as out:
            vm.artifact(self.artifact_args(str(movie)), True)
        report = json.loads(out.getvalue())
        self.assertEqual(report["timeline"], str(self.state / "take.timeline.json"))
        self.assertEqual(json.loads((self.state / "take.timeline.json").read_text()), report["marks"])
        self.assertEqual([m["text"] for m in report["marks"]], ["first mark", "second mark"])

    def test_pause_never_signals_a_helper_that_a_user_signal_would_kill(self):
        # A helper before 4 has no handler for SIGUSR1, so it would end without saving the movie.
        vm = self.vm()
        vm.ctl = "/Users/admin/.local/bin/vmctl"
        for action, marker in [("pause", "/usr/bin/touch /tmp/lampvm-record-paused"), ("resume", "/bin/rm -f /tmp/lampvm-record-paused")]:
            with mock.patch.object(vm, "need"), mock.patch.object(vm, "helper_version", return_value=3), \
                 mock.patch.object(cli.subprocess, "run") as run:
                with self.assertRaisesRegex(cli.Error, "cannot pause"):
                    vm.signal_recording(action)
            run.assert_not_called()
            with mock.patch.object(vm, "need"), mock.patch.object(vm, "helper_version", return_value=4), contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                vm.signal_recording(action)
            sent = run.call_args.args[0]
            self.assertEqual(sent[1:5], ["exec", vm.name, "/bin/zsh", "-c"])
            self.assertTrue(sent[5].startswith(marker + " && exec /usr/bin/pkill -USR1 -f "))

    def test_system_prompts_read_from_real_guest_trees(self):
        # `vmctl tree SecurityAgent` in a macOS 26 guest on 2026-10-15: an admin password prompt, then a Keychain prompt.
        admin = ("AXWindow\t720,306\t260x312\tUntitled\nAXStaticText\t720,258\t218x18\tosascript\n"
                 "AXStaticText\t720,290\t218x18\tosascript wants to make changes.\nAXStaticText\t720,322\t218x18\tEnter your password to allow this.\n"
                 "AXTextField\t720,358\t232x26\tManaged via Tart | Username\nAXTextField\t720,390\t232x26\tPassword\n"
                 "AXButton\t661,432\t113x30\tCancel\nAXButton\t778,432\t113x30\tOK\n")
        keychain = ("AXWindow\t720,276\t434x186\t_NS:82\nAXImage\t555,235\t66x66\tsecurity | _NS:47\n"
                    "AXStaticText\t758,227\t320x50\tsecurity wants to use your confidential information stored in \u201clampvm-made-up-item\u201d in your keychain. | _NS:73\n"
                    "AXStaticText\t758,266\t320x16\tTo allow this, enter the \u201clogin\u201d keychain password. | infotext\n"
                    "AXStaticText\t630,293\t64x18\tPassword: | _NS:99\nAXTextField\t793,293\t250x26\t_NS:105\n"
                    "AXButton\t535,337\t26x26\tHelp | _NS:42\nAXButton\t650,337\t104x26\tAlways Allow | _NS:21\n"
                    "AXButton\t786,337\t81x26\tDeny | _NS:29\nAXButton\t877,337\t81x26\tAllow | _NS:8\n")
        self.assertEqual(cli.prompt_from_tree(admin), {"text": ["osascript", "osascript wants to make changes.", "Enter your password to allow this."], "buttons": ["Cancel", "OK"]})
        self.assertEqual(cli.prompt_from_tree(keychain)["buttons"], ["Help", "Always Allow", "Deny", "Allow"])
        self.assertEqual(cli.prompt_from_tree(keychain)["text"][1], "To allow this, enter the \u201clogin\u201d keychain password.")
        self.assertIsNone(cli.prompt_from_tree(""))

    def test_notification_banner_found_from_real_guest_tree(self):
        # `vmctl tree com.apple.notificationcenterui --json` in a macOS 26 guest on 2026-10-15, with a notification
        # permission banner up, trimmed to its windows. The banner's top edge covered a window starting at y 108.
        items = json.loads((ROOT / "tests/fixtures/notification-center-banner.json").read_text())
        found = cli.banners_from_tree(items)
        self.assertEqual([b["bounds"] for b in found], [{"x": 1080, "y": 46, "width": 344, "height": 73}])
        self.assertTrue(found[0]["text"].startswith("\u201cLampVM Harness\u201d Notifications"))
        self.assertTrue(cli.overlaps(found[0]["bounds"], {"x": 1060, "y": 108, "width": 360, "height": 332}))
        self.assertFalse(cli.overlaps(found[0]["bounds"], {"x": 140, "y": 48, "width": 900, "height": 692}))
        widgets_only = [e for e in items if not e["path"].startswith("0.0")]
        self.assertEqual(cli.banners_from_tree(widgets_only), [])

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
