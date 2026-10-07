#!/usr/bin/env python3
"""Exercise real guest behavior on an already prepared private running VM.

Usage: tests/smoke.py --name OWNED_VM --state-dir STATE --proof-dir OUTSIDE_REPO
This deliberately never creates, starts, stops, resets, or destroys a VM.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
APP = "LampVM Harness"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    proof = args.proof_dir.expanduser().resolve()
    if proof == ROOT or ROOT in proof.parents:
        raise SystemExit("Proof must be outside the checkout")
    proof.mkdir(parents=True, exist_ok=True)
    base = [str(ROOT / "bin/lampvm"), "--name", args.name, "--state-dir", args.state_dir]
    log = (proof / "commands.txt").open("w")

    def call(*command, expect=0):
        result = subprocess.run(base + list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
        entry = "$ lampvm " + " ".join(command) + "\n" + result.stdout + "exit: " + str(result.returncode) + "\n"
        print(entry, flush=True)
        log.write(entry); log.flush()
        if result.returncode != expect:
            raise RuntimeError("Unexpected exit for " + command[0])
        return result.stdout

    def result_json(text):
        # Activation diagnostics can precede a final artifact report.
        for line in reversed(text.strip().splitlines()):
            try:
                return json.loads(line)
            except ValueError:
                pass
        raise RuntimeError("Command did not return JSON")

    def shot(name):
        return call("shot", str(proof / (name + ".png")), "--app", APP)

    def point(text):
        matches = json.loads(call("find", APP, text, "--exact", "--json"))
        if len(matches) != 1:
            raise RuntimeError("Harness selector isn't unique: " + text)
        rect = matches[0]["bounds"]
        return str(rect["x"] + rect["width"] / 2), str(rect["y"] + rect["height"] / 2)

    try:
        subprocess.run([str(ROOT / "tests/build-harness.sh")], check=True)
        call("install", str(ROOT / "build" / (APP + ".app")))
        call("launch", APP)
        call("wait", APP, "Click Me", "--exact", "--timeout", "10")
        call("exec", "/usr/bin/sw_vers")
        call("doctor")
        apps = json.loads(call("apps"))
        if not any(item["name"] == APP for item in apps):
            raise RuntimeError("Running app discovery missed the harness")
        call("tree", APP, "--json")
        call("windows", APP)
        call("bounds", APP)
        shot("01-baseline")
        recognized = result_json(call("ocr", "--app", APP))
        if not any("Click Me" in line["text"] for line in recognized["lines"]):
            raise RuntimeError("OCR did not recognize the harness button")
        shot("01-ocr")
        offscreen = result_json(call("clickon", APP, "Row 60 — test content", "--exact", expect=1))
        if offscreen.get("landed") or offscreen.get("eventPosted"):
            raise RuntimeError("Offscreen click should be rejected before posting events")

        # A different frontmost app ensures clickon proves activation as well as hit testing.
        call("launch", "TextEdit")
        time.sleep(1)
        clicked = result_json(call("clickon", APP, "Click Me", "--exact"))
        if not clicked["landed"] or not clicked["eventPosted"]:
            raise RuntimeError("Label click didn't land")
        call("assert", APP, "Clicks: 1", "--exact")
        shot("02-activated-click")
        hitx, hity = point("Click Me")
        hit = result_json(call("hit", hitx, hity))
        if hit.get("role") != "AXButton" or "Click Me" not in hit.get("label", ""):
            raise RuntimeError("Hit diagnostics missed the button")
        shot("02-hit-diagnostics")
        pressed = result_json(call("press", APP, "Click Me", "--exact"))
        if not pressed.get("performed") or pressed.get("method") != "AXPress":
            raise RuntimeError("Accessibility press didn't perform")
        call("assert", APP, "Clicks: 2", "--exact")
        shot("02-accessibility-press")

        call("clickon", APP, "Type here", "--exact")
        call("type", "Agent input 🧪")
        call("assert", APP, "Typed: Agent input 🧪", "--exact")
        shot("03-unicode-typing")
        call("key", "a", "cmd")
        call("type", "--", "--app")
        call("assert", APP, "Typed: --app", "--exact")
        call("setvalue", APP, "Type here", "Direct value 🧪", "--exact")
        call("assert", APP, "Direct value 🧪")
        shot("04-setvalue")

        call("assert", APP, "Delayed element ready", "--absent", "--exact")
        call("assert", APP, "Element never present", "--exact", expect=1)
        call("wait", APP, "Element never present", "--timeout", "0.2", expect=1)
        call("clickon", APP, "Reveal Delayed", "--exact")
        call("wait", APP, "Delayed element ready", "--exact", "--timeout", "5")
        shot("05-wait-present")
        call("clickon", APP, "Hide Delayed", "--exact")
        call("wait", APP, "Delayed element ready", "--absent", "--exact", "--timeout", "5")
        shot("06-wait-absent")

        sx, sy = point("Scroll rows")
        call("scroll", sx, sy, "-650", "--app", APP)
        offsets = json.loads(call("find", APP, "Scroll offset:", "--json"))
        if not offsets or any(item["label"] == "Scroll offset: 0" for item in offsets):
            raise RuntimeError("Scroll didn't change the viewport")
        shot("07-scroll-down")
        call("scroll", sx, sy, "650", "--app", APP)
        call("assert", APP, "Scroll offset: 0", "--exact")
        shot("08-scroll-up")

        x1, y1 = point("Drag source")
        x2, y2 = point("Drop target")
        call("drag", x1, y1, x2, y2, "0.6", "--app", APP)
        call("assert", APP, "Drag landed", "--exact")
        shot("09-drag")

        cx, cy = point("Context target")
        call("rightclick", cx, cy, "--app", APP)
        call("assert", APP, "Context menu opened", "--exact")
        shot("10-right-click")
        call("key", "escape")
        call("clickon", APP, "Context target", "--exact", "--right")
        call("clickon", APP, "Choose context action", "--exact")
        call("assert", APP, "Context action chosen", "--exact")
        shot("11-context-action")

        bx, by = point("Click Me")
        call("move", bx, by, "--app", APP)
        call("doubleclick", bx, by, "--app", APP)
        call("click", bx, by, "--app", APP)
        shot("12-raw-mouse")
        call("activate", APP)

        # Two concurrent requests have independent guest names and independent host artifacts.
        with ThreadPoolExecutor(max_workers=2) as pool:
            captures = list(pool.map(lambda _: result_json(call("shot", "--app", APP)), range(2)))
        if captures[0]["file"] == captures[1]["file"]:
            raise RuntimeError("Capture names collided")
        for index, capture in enumerate(captures):
            source = Path(capture["file"])
            (proof / ("13-concurrent-" + str(index) + ".png")).write_bytes(source.read_bytes())

        recording_path = proof / "14-recording.mov"
        recording = subprocess.Popen(base + ["record", str(recording_path), "--seconds", "8", "--app", APP], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2)
        call("clickon", APP, "Click Me", "--exact")
        call("clickon", APP, "Reveal Delayed", "--exact")
        call("wait", APP, "Delayed element ready", "--exact", "--timeout", "5")
        stdout, _ = recording.communicate(timeout=60)
        log.write("$ lampvm record --seconds 8 --app Harness\n" + stdout + "exit: " + str(recording.returncode) + "\n"); log.flush()
        print(stdout)
        if recording.returncode:
            raise RuntimeError("Recording failed")
        result_json(stdout)
        if not recording_path.is_file() or recording_path.stat().st_size < 1000:
            raise RuntimeError("Recording is empty")
        shot("15-recorded-final-state")
        call("find", APP, "Element never present", "--json", expect=1)
        call("clickon", APP, "Element never present", expect=1)
        call("quit", APP)
        call("wait", APP, "Click Me", "--absent", "--timeout", "5")
        call("launch", APP)
        call("wait", APP, "Click Me", "--exact", "--timeout", "5")
        shot("16-graceful-relaunch")
        (proof / "smoke-result.json").write_text(json.dumps({"ok": True, "vm": args.name, "app": APP, "screenshots": sorted(p.name for p in proof.glob("*.png")), "recording": recording_path.name}, indent=2) + "\n")
        print("Real VM smoke proof passed", flush=True)
    finally:
        log.close()


if __name__ == "__main__":
    main()
