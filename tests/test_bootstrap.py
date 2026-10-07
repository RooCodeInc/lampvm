import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).resolve().parents[1] / "lib/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def element(role, label):
    return {"role": role, "label": label, "bounds": {"x": 0, "y": 0, "width": 100, "height": 30}, "enabled": True}


def page(identifier, *buttons):
    return [element("AXWindow", identifier)] + [element("AXButton", label) for label in buttons]


class DesktopBootstrap(unittest.TestCase):
    def test_real_guest_page_fixtures_choose_only_decline_actions(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/bootstrap-pages.json").read_text())
        for name, fixture in fixtures.items():
            with self.subTest(page=name):
                self.assertEqual(bootstrap.action_for(fixture["elements"]), fixture["action"])

    def test_known_post_update_actions(self):
        cases = [
            (page("AutoUpdate", "Only Download Automatically", "Continue"), "Only Download Automatically"),
            (page("iCloudLogin", "Other Sign-In Options", "Continue"), "Other Sign-In Options"),
            (page("iCloudLogin", "Sign in Later", "Sign In"), "Sign in Later"),
        ]
        for elements, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(bootstrap.action_for(elements)["label"], expected)

    def test_observed_filevault_skips_without_enabling_encryption(self):
        elements = page("Window | iCloudEnableFDEUpsell", "Not Now | Cancel Button", "Turn On | Next Button")
        elements.append(element("AXStaticText", "Your Mac is Ready for FileVault"))
        self.assertEqual(bootstrap.action_for(elements), {"label": "Not Now", "role": "AXButton"})
        self.assertIsNone(bootstrap.action_for(page("FileVault", "Continue", "Turn On")))

    def test_observed_icloud_menu_and_label_whitespace(self):
        # Reduced from the real post-update guest Accessibility tree.
        elements = page("Window | iCloudLogin", "Other Sign-In Options  | Alternate Button")
        elements.append(element("AXStaticText", "Sign In to Your Apple Account"))
        self.assertEqual(bootstrap.action_for(elements), {"label": "Other Sign-In Options ", "role": "AXButton"})
        elements[1]["enabled"] = False
        elements.append(element("AXMenuItem", "Sign in Later in Settings | userDeclinediCloud"))
        self.assertEqual(bootstrap.action_for(elements), {"label": "Sign in Later in Settings", "role": "AXMenuItem"})

    def test_unknown_sign_in_legal_security_and_privacy_pages_have_no_action(self):
        for identifier in ("Terms and Conditions", "Administrator Password", "Enable Siri", "Diagnostic Consent", "Unknown Welcome", "Unknown Apple Account"):
            with self.subTest(identifier=identifier):
                self.assertIsNone(bootstrap.action_for(page(identifier, "Continue", "Allow", "Sign In", "Get Started")))
        # A known underlying page cannot authorize an unrelated sheet's Skip.
        unknown = page("iCloudLogin", "Skip", "Other Sign-In Options")
        unknown.append(dict(element("AXSheet", "Unexpected warning"), path="0.0.5"))
        self.assertIsNone(bootstrap.action_for(unknown))

    def test_duplicate_or_disabled_buttons_are_not_clicked(self):
        self.assertIsNone(bootstrap.action_for(page("AutoUpdate", "Only Download Automatically", "Only Download Automatically")))
        elements = page("AutoUpdate", "Only Download Automatically")
        elements[-1]["enabled"] = False
        self.assertIsNone(bootstrap.action_for(elements))

    def test_unknown_window_fails_without_click(self):
        tree = SimpleNamespace(returncode=0, stdout=json.dumps(page("Unknown Security", "Allow", "Continue")), stderr="")
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(bootstrap.subprocess, "run", return_value=tree) as run, patch.object(bootstrap.time, "monotonic", side_effect=lambda: clock[0]), patch.object(bootstrap.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "no button was clicked"):
                bootstrap.complete_desktop("tart", "private", "/guest/vmctl", timeout=20)
        self.assertTrue(all(call.args[0][-3:] == ["tree", bootstrap.APP, "--json"] for call in run.call_args_list))

    def test_observed_flow_stops_when_assistant_finishes(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/bootstrap-pages.json").read_text())
        pages = [page("AutoUpdate", "Only Download Automatically"), page("iCloudLogin", "Other Sign-In Options")] + [fixtures[key]["elements"] for key in ["apple-options", "apple-skip", "filevault", "filevault-decline", "finished"]]
        state = {"clock": 0, "page": 0, "underlay": 2}
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if "clickon" in command or "press" in command:
                state["page"] += 1
                payload = '{"performed":true,"method":"AXPress"}' if "press" in command else '{"landed":true,"eventPosted":true}'
                return SimpleNamespace(returncode=0, stdout=payload, stderr="")
            if state["page"] == len(pages):
                return SimpleNamespace(returncode=1, stdout="", stderr="com.apple.SetupAssistant is not running")
            if state["page"] == len(pages) - 1 and state["underlay"]:
                state["underlay"] -= 1
                # Continue has landed, but an old FileVault underlay remains briefly.
                return SimpleNamespace(returncode=0, stdout=json.dumps(fixtures["filevault"]["elements"]), stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(pages[state["page"]]), stderr="")
        def sleep(seconds):
            state["clock"] += seconds
        with patch.object(bootstrap.subprocess, "run", side_effect=run), patch.object(bootstrap.time, "monotonic", side_effect=lambda: state["clock"]), patch.object(bootstrap.time, "sleep", side_effect=sleep):
            labels = bootstrap.complete_desktop("tart", "private", "/guest/vmctl")
        self.assertEqual(labels, ["Only Download Automatically", "Other Sign-In Options", "Sign in Later in Settings", "Skip", "Not Now", "Continue", "Get Started"])
        clicks = [command for command in commands if "clickon" in command or "press" in command]
        self.assertTrue(all(command[5] == bootstrap.APP and "--exact" in command and "--role" in command for command in clicks))
        self.assertEqual(clicks[-1][4], "press")
        self.assertTrue(all(command[4] == "clickon" for command in clicks[:-1]))

    def test_semantic_completion_requires_axpress_result(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/bootstrap-pages.json").read_text())
        def run(command, **kwargs):
            if "press" in command:
                # A mouse event report cannot prove a semantic Accessibility press.
                return SimpleNamespace(returncode=0, stdout='{"landed":true,"eventPosted":true}', stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(fixtures["finished"]["elements"]), stderr="")
        with patch.object(bootstrap.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "Accessibility press was not performed"):
                bootstrap.complete_desktop("tart", "private", "/guest/vmctl")


if __name__ == "__main__":
    unittest.main()
