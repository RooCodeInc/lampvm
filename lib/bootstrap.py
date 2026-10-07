"""Finish known public-image Setup Assistant pages with verified UI actions."""
import json
import subprocess
import time

APP = "com.apple.SetupAssistant"


class BootstrapError(RuntimeError):
    pass


def components(element):
    return [part.strip() for part in element.get("label", "").split(" | ") if part.strip()]


def action_for(elements):
    """Only choose actions on a recognized page; never guess a generic Continue."""
    windows = {label for element in elements if element.get("role") == "AXWindow" for label in components(element)}

    def button(label, roles=("AXButton",), parent_path=None):
        candidates = [element for element in elements if label in components(element) and element.get("role") in roles and element.get("enabled", True)]
        if parent_path is not None:
            candidates = [item for item in candidates if item.get("path", "").startswith(parent_path + ".")]
        if len(candidates) != 1:
            return None
        bounds = candidates[0].get("bounds", {})
        if bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0:
            return None
        # Accessibility labels retain spaces that vmctl's exact matcher sees.
        actual = next(part for part in candidates[0]["label"].split(" | ") if part.strip() == label)
        return dict(label=actual, role=candidates[0]["role"])

    if "AutoUpdate" in windows:
        return button("Only Download Automatically")
    if "iCloudLogin" in windows:
        # The confirmation sheet takes precedence over its underlying sign-in page.
        sheets = [item for item in elements if item.get("role") == "AXSheet"]
        if sheets:
            for sheet in sheets:
                prefix = sheet.get("path", "")
                sheet_labels = {label for item in elements if item.get("path", "").startswith(prefix + ".") for label in components(item)}
                if "Are you sure you want to skip signing in with an Apple Account?" in sheet_labels:
                    return button("Skip", parent_path=prefix)
            return None
        if result := button("Sign in Later in Settings", ("AXMenuItem",)):
            return result
        for label in ("Sign in Later", "Other Sign-In Options"):
            if result := button(label):
                return result
    if "iCloudEnableFDEUpsell" in windows:
        # The observed final welcome overlay leaves the FileVault window in its tree.
        for window in elements:
            bounds = window.get("bounds", {})
            if window.get("role") == "AXWindow" and not window.get("label") and bounds.get("x") == 0 and bounds.get("y") == 0 and bounds.get("width", 0) >= 640 and bounds.get("height", 0) >= 480:
                if result := button("Get Started", parent_path=window.get("path", "")):
                    result["method"] = "press"
                    return result
        sheets = [item for item in elements if item.get("role") == "AXSheet"]
        if sheets:
            for sheet in sheets:
                prefix = sheet.get("path", "")
                sheet_labels = {label for item in elements if item.get("path", "").startswith(prefix + ".") for label in components(item)}
                if "Are you sure you want to continue without FileVault? You can manage this at any time in Privacy & Security settings." in sheet_labels:
                    return button("Continue", parent_path=prefix)
            return None
        return button("Not Now")
    return None


def complete_desktop(tart, name, ctl, timeout=120, *, allow_semantic_completion=True):
    """Return observed actions; unknown panes remain intact and fail explicitly."""
    deadline = time.monotonic() + timeout
    absent_since = None
    unknown_since = None
    last_click = None
    clicked_at = None
    actions = []
    expected_next = {
        "Only Download Automatically": {"Other Sign-In Options", "Sign in Later in Settings", "Skip", "Not Now", "Get Started"},
        "Other Sign-In Options": {"Sign in Later in Settings", "Sign in Later"},
        "Sign in Later in Settings": {"Skip"},
        "Sign in Later": {"Skip"},
        "Skip": {"Not Now", "Get Started"},
        "Not Now": {"Continue"},
        "Continue": {"Get Started"},
        "Get Started": set(),
    }

    def guest(*args):
        return subprocess.run([str(tart), "exec", name, ctl, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)

    while time.monotonic() < deadline:
        tree = guest("tree", APP, "--json")
        if tree.returncode:
            if "is not running" not in tree.stderr:
                raise BootstrapError("Cannot inspect Setup Assistant: " + tree.stderr.strip())
            elements = []
        else:
            try:
                elements = json.loads(tree.stdout)
            except (ValueError, TypeError) as error:
                raise BootstrapError("Setup Assistant returned an invalid Accessibility tree") from error
            if not isinstance(elements, list) or any(not isinstance(element, dict) for element in elements):
                raise BootstrapError("Setup Assistant returned an invalid Accessibility tree")
        visible = [element for element in elements if element.get("role") == "AXWindow" and element.get("bounds", {}).get("width", 0) > 0 and element.get("bounds", {}).get("height", 0) > 0]
        if not visible:
            absent_since = absent_since or time.monotonic()
            # Assistant can launch a little after the desktop; require stable absence.
            if time.monotonic() - absent_since >= 8:
                if actions and actions[-1].strip() == "Get Started":
                    print("setup desktop: verified Setup Assistant finished (stable absence)", flush=True)
                return actions
            time.sleep(0.5)
            continue
        absent_since = None
        action = action_for(elements)
        # Status-bar clocks and menu bookkeeping change without a page transition.
        fingerprint = json.dumps([(element.get("role"), element.get("label"), element.get("path")) for element in elements if element.get("role") in ("AXWindow", "AXSheet")], sort_keys=True)
        if action is None:
            unknown_since = unknown_since or time.monotonic()
            if time.monotonic() - unknown_since >= 10:
                labels = sorted({label for element in elements for label in components(element)})
                raise BootstrapError("Unsupported Setup Assistant page; no button was clicked. Inspect tree/shot for Setup Assistant and complete it explicitly: " + "; ".join(labels)[:1200])
            time.sleep(0.5)
            continue
        unknown_since = None
        if actions and action["label"].strip() not in expected_next[actions[-1].strip()]:
            # Trees can briefly expose an enabled underlay during page animation.
            # Wait for the next decline action instead of clicking a stale button.
            if time.monotonic() - clicked_at > 10:
                raise BootstrapError("Setup Assistant did not reach the expected page after " + actions[-1].strip() + "; inspect its window before continuing")
            time.sleep(0.5)
            continue
        if last_click == (fingerprint, action):
            if time.monotonic() - clicked_at > 10:
                raise BootstrapError("Setup Assistant did not change after clicking " + action["label"] + "; inspect its window before continuing")
            time.sleep(0.5)
            continue
        semantic = action.get("method") == "press"
        if semantic and not allow_semantic_completion:
            raise BootstrapError("Known final Get Started control requires an Accessibility press, disabled for this invocation")
        clicked = guest("press" if semantic else "clickon", APP, action["label"], "--exact", "--role", action["role"])
        try:
            result = json.loads(clicked.stdout)
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        verified = result.get("performed") is True and result.get("method") == "AXPress" if semantic else result.get("landed") is True and result.get("eventPosted") is True
        if clicked.returncode or not verified:
            message = "Setup Assistant Accessibility press was not performed: " if semantic else "Setup Assistant click did not land: "
            raise BootstrapError(message + action["label"] + "; " + (clicked.stderr or clicked.stdout).strip())
        if semantic:
            print("setup desktop: performed Accessibility press on Get Started; waiting to verify Assistant absence", flush=True)
        else:
            print("setup desktop: clicked " + action["label"] + " (hit-test verified)", flush=True)
        actions.append(action["label"])
        last_click = (fingerprint, action)
        clicked_at = time.monotonic()
        time.sleep(1)
    raise BootstrapError("Setup Assistant did not finish within " + str(timeout) + " seconds; inspect its window")
