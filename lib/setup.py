#!/usr/bin/env python3
"""Prepare an owned macOS VM; never modify a source VM or stop somebody else's VM."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from bootstrap import complete_desktop
STATE = Path(os.environ.get("LAMPVM_STATE_DIR", str(Path.home() / "Library/Application Support/lampvm"))).expanduser()
VERSION = "2.32.1"
TART_SHA256 = "8554ab4f7fc12afe52f9b7e3093a935673cbac737a83973d2db7a0683c814529"
RELEASE = "https://github.com/openai/tart/releases/download/2.32.1"
IMAGE = "ghcr.io/cirruslabs/macos-tahoe-base@sha256:87f3aa5ce21b5c876268f233bdfecf38b4c2a8116fe9bbb718e714cbae187377"


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def capture(args, **kwargs):
    return run(args, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
        raise RuntimeError("VM names must start with a letter or digit, contain only letters, digits, dot, underscore or hyphen, and be at most 80 characters")
    return name


def load_config(runtime_config, source):
    config = json.loads(runtime_config.read_text()) if runtime_config.is_file() else {}
    if not isinstance(config, dict):
        raise RuntimeError("Runtime config must be a JSON object")
    if source != runtime_config:
        if not source.is_file():
            raise RuntimeError("Config file does not exist: " + str(source))
        overlay = json.loads(source.read_text())
        if not isinstance(overlay, dict):
            raise RuntimeError("Config must be a JSON object")
        config.update(overlay)
    return config


def validate_config(config):
    if type(config["cpu"]) is not int or not 1 <= config["cpu"] <= (os.cpu_count() or 1):
        raise RuntimeError("cpu must be a positive integer no greater than the host CPU count")
    if type(config["memory"]) is not int or not 2048 <= config["memory"] <= 1048576:
        raise RuntimeError("memory must be an integer in MB between 2048 and 1048576")
    display = config["display"]
    match = re.fullmatch(r"([0-9]+)x([0-9]+)", display) if isinstance(display, str) else None
    if not match or not 640 <= int(match[1]) <= 8192 or not 480 <= int(match[2]) <= 8192:
        raise RuntimeError("display must be WIDTHxHEIGHT with width 640–8192 and height 480–8192")
    image = config["image"]
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+|@sha256:[a-f0-9]{64})", image):
        raise RuntimeError("image must be an OCI reference with a tag or sha256 digest; use --from for a local VM")


def check_owned(vm, name, marker, *, clean_source=None):
    if not vm.exists(name):
        return
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict) or record.get("name") != name:
        raise RuntimeError("VM " + name + " exists and is not owned by this lampvm state; choose a different name")
    if clean_source and (record.get("role") != "clean" or record.get("source") != clean_source):
        raise RuntimeError("Clean VM " + name + " does not belong to working VM " + clean_source + "; choose another clean name")


@contextlib.contextmanager
def lock(name):
    path = STATE / "locks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def download(url, destination):
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)


def tart_binary(no_download=False):
    override = os.environ.get("LAMPVM_TART")
    if override:
        tart = Path(override).expanduser()
        if not tart.is_file():
            raise RuntimeError("LAMPVM_TART must point to Tart's executable inside tart.app")
        return tart
    tart = STATE / "tart/tart.app/Contents/MacOS/tart"
    if tart.is_file():
        return tart
    if no_download:
        raise RuntimeError("Tart is not installed; omit --no-download or set LAMPVM_TART")
    print("Downloading Tart 2.32.1 (about 22 MB), checking its pinned release checksum", flush=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tart-install-", dir=STATE) as temporary:
        temporary = Path(temporary)
        archive = temporary / "tart.tar.gz"
        checksums = temporary / "checksums.txt"
        download(RELEASE + "/tart_2.32.1_checksums.txt", checksums)
        if not any(line.split() == [TART_SHA256, "tart.tar.gz"] for line in checksums.read_text().splitlines()):
            raise RuntimeError("Published Tart checksum differs from the pinned checksum; refusing installation")
        download(RELEASE + "/tart.tar.gz", archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != TART_SHA256:
            raise RuntimeError("Tart download failed SHA-256 verification")
        with tarfile.open(archive) as tar:
            # The trusted checksum identifies the upstream archive; still reject path traversal.
            base = temporary.resolve()
            for member in tar.getmembers():
                target = (temporary / member.name).resolve()
                if target != base and not str(target).startswith(str(base) + os.sep):
                    raise RuntimeError("Unsafe path in Tart archive")
            tar.extractall(temporary)
        app = temporary / "tart.app"
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
        dest = STATE / "tart"
        dest.mkdir(exist_ok=True)
        app.replace(dest / "tart.app")
        (dest / "LICENSE.txt").write_text((ROOT / "licenses/Tart-2.32.1.txt").read_text())
    return tart


class VM:
    def __init__(self, tart, name):
        self.tart, self.name = tart, name
        self.process = None

    def command(self, *args, **kwargs):
        env = os.environ.copy()
        env["TART_NO_AUTO_PRUNE"] = "1"
        kwargs.setdefault("env", env)
        return run([self.tart, *args], **kwargs)

    def all(self):
        return json.loads(capture([self.tart, "list", "--format", "json"]))

    def exists(self, name):
        return any(v["Name"] == name and v.get("Source") == "local" for v in self.all())

    def running(self, name=None):
        return any(v["Name"] == (name or self.name) and v.get("Running") for v in self.all())

    def guest(self, *args, **kwargs):
        return self.command("exec", self.name, *args, **kwargs)

    def text(self, *args):
        return capture([self.tart, "exec", self.name, *args])

    def script(self, source):
        return self.command("exec", "-i", self.name, "/bin/zsh", "-se", input=source.encode())

    def start(self, wait_slot):
        deadline = time.monotonic() + wait_slot
        while True:
            with lock("host-start.lock"):
                busy = [v for v in self.all() if v.get("Running")]
                # Also count Tart runners whose VM hasn't reached Running yet.
                processes = subprocess.run(["/usr/bin/pgrep", "-f", r"/tart run"], stdout=subprocess.PIPE, text=True)
                count = max(len(busy), len(processes.stdout.splitlines()))
                if count < 2:
                    logfile = STATE / "run" / (self.name + "-setup.log")
                    logfile.parent.mkdir(parents=True, exist_ok=True)
                    with logfile.open("ab") as output:
                        self.process = subprocess.Popen([str(self.tart), "run", "--no-graphics", "--no-audio", "--no-clipboard", self.name], stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    boot_deadline = time.monotonic() + 120
                    while time.monotonic() < boot_deadline:
                        if self.process.poll() is not None:
                            raise RuntimeError("Tart exited while starting; inspect " + str(logfile))
                        try:
                            self.guest("/usr/bin/true", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                            return
                        except (subprocess.SubprocessError, OSError):
                            time.sleep(1)
                    raise RuntimeError("Guest agent did not answer; inspect " + str(logfile))
            if time.monotonic() >= deadline:
                raise RuntimeError("Both macOS VM slots are occupied. No VM was stopped. Retry setup --wait-slot SECONDS when a slot is available")
            print("Waiting for a free macOS VM slot; existing VMs remain untouched", flush=True)
            time.sleep(min(10, max(1, deadline - time.monotonic())))

    def stop(self):
        if not self.running():
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=10)
            return
        with contextlib.suppress(subprocess.SubprocessError):
            self.guest("/usr/bin/sudo", "-n", "/sbin/shutdown", "-h", "now", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        for _ in range(40):
            if not self.running():
                return
            time.sleep(1)
        self.command("stop", self.name)


def configure_vm(vm, config, fresh=False):
    args = ["set", vm.name, "--memory", str(config["memory"]), "--cpu", str(config["cpu"]), "--display", str(config["display"])]
    # ECID changes invalidate the hardware identity underlying existing guest state.
    # Make new clones distinct, but preserve that identity across setup retries.
    if fresh:
        args.extend(["--random-mac", "--random-serial"])
    vm.command(*args)


def panic_reports(vm):
    reports = vm.text("/bin/zsh", "-c", 'for report in /Library/Logs/DiagnosticReports/*panic*(N) "$HOME"/Library/Logs/DiagnosticReports/*panic*(N); do [[ ! -f "$report" ]] || print -r -- "$report"; done')
    return set(reports.splitlines())


def check_guest_health(vm, ctl, known_reports=()):
    reports = panic_reports(vm) - set(known_reports)
    if reports:
        raise RuntimeError("Guest created kernel panic reports during setup: " + ", ".join(sorted(reports)) + ". Inspect the guest reports or choose another base image with a new VM name. No clean snapshot will be created")
    reporter = subprocess.run([str(vm.tart), "exec", vm.name, ctl, "tree", "Diagnostics Reporter"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if "restarted because of a problem" in reporter.stdout.lower():
        raise RuntimeError("Guest reports that it restarted because of a problem. Inspect its diagnostic reports or choose another base image with a new VM name. No clean snapshot will be created")


PROVISION = r'''
tool="$HOME/.local/bin/vmctl"
os_version=$(/usr/bin/sw_vers -productVersion)
os_build=$(/usr/bin/sw_vers -buildVersion)
# These are disposable guest settings; never host settings.
/usr/bin/defaults write com.apple.SetupAssistant DidSeeCloudSetup -bool true
/usr/bin/defaults write com.apple.SetupAssistant DidSeeSyncSetup -bool true
/usr/bin/defaults write com.apple.SetupAssistant DidSeeSiriSetup -bool true
/usr/bin/defaults write com.apple.SetupAssistant DidSeePrivacy -bool true
/usr/bin/defaults write com.apple.SetupAssistant DidSeeAppearanceSetup -bool true
/usr/bin/defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$os_version"
/usr/bin/defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$os_build"
/usr/bin/defaults write com.apple.SetupAssistant MiniBuddyLaunchedPostMigration -bool true
/usr/bin/defaults -currentHost write com.apple.screensaver idleTime -int 0
/usr/bin/defaults write com.apple.screensaver askForPassword -int 0
/usr/bin/defaults write com.apple.Terminal NSQuitAlwaysKeepsWindows -bool false
/usr/bin/sudo -n /usr/bin/pmset -a sleep 0 displaysleep 0 disksleep 0
/usr/bin/sudo -n /usr/bin/defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticDownload -bool false
/usr/bin/sudo -n /usr/bin/defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
/usr/bin/sudo -n /usr/bin/defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool false
# Tart base images use public admin/admin auto-login and passwordless sudo already.
# Preserve their credentials rather than copying any host user or keychain.
if [[ "$os_version" == 27* ]]; then
  userdb=$(/usr/bin/sudo -n /usr/sbin/lsof -a -u "$UID" -c tccd -Fn | /usr/bin/sed -n 's|^n\(/private/var/containers/Data/ProtectedSystem/.*com.apple.TCC/TCC.db\)$|\1|p' | /usr/bin/head -1)
else
  userdb="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
fi
[[ -n "$userdb" ]] && /usr/bin/sudo -n /bin/test -f "$userdb" || { echo 'Guest user TCC database is missing; base image must have a logged-in desktop' >&2; exit 1; }
agent=/opt/homebrew/bin/tart-guest-agent
agent=${agent:A}
[[ -x "$agent" ]] || { echo 'Tart guest agent executable is missing' >&2; exit 1; }
# SIP-disabled public testing images permit this; normal secured macOS does not.
# Explicitly grant only the installed helper, guest agent, and capture executable.
for db in '/Library/Application Support/com.apple.TCC/TCC.db' "$userdb"; do
  /usr/bin/sudo -n /bin/test -f "$db"
  for client in "$tool" "$agent" /usr/sbin/screencapture; do
    for service in kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceScreenCapture; do
      quoted_client=${client//\'/\'\'}
      /usr/bin/sudo -n /usr/bin/sqlite3 "$db" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,indirect_object_identifier_type,indirect_object_identifier) VALUES ('$service','$quoted_client',1,2,0,1,NULL,'UNUSED');"
    done
  done
done
# Re-read grants for future commands without interrupting the guest-agent connection.
/usr/bin/killall cfprefsd 2>/dev/null || true
echo 'Prepared guest desktop settings and automation permissions'
'''


def main():
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="Runtime state directory; default LAMPVM_STATE_DIR or user Application Support/lampvm")
    parser.add_argument("--config", type=Path, help="JSON config; default LAMPVM_CONFIG or state/config.json")
    parser.add_argument("--name", help="Working VM name (new owned VM only)")
    parser.add_argument("--from", dest="source", help="Clone a stopped local base VM, preserving it")
    parser.add_argument("--image", help="Public Tart OCI image, preferably @sha256 digest")
    parser.add_argument("--no-download", action="store_true", help="Use an already installed Tart")
    parser.add_argument("--wait-slot", type=int, default=0, help="Wait this many seconds for a free slot; never stop another VM")
    parser.add_argument("--clean", help="Clean image name to prepare instead of the configured one")
    parser.add_argument("--no-build", action="store_true", help="Use the helpers already built in the state directory")
    parser.add_argument("--keep-config", action="store_true", help="Leave runtime config.json as it is, for a prepared image other than the default")
    args = parser.parse_args()
    if args.state_dir:
        STATE = args.state_dir.expanduser().resolve()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("lampvm needs an Apple Silicon Mac")
    version = capture(["/usr/bin/sw_vers", "-productVersion"])
    if int(version.split(".")[0]) < 14:
        raise RuntimeError("macOS 14 or newer is required for Tart exec")
    if subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError("Install Apple's Command Line Tools: xcode-select --install; complete the installer, then rerun setup")
    runtime_config = STATE / "config.json"
    config_path = (args.config or Path(os.environ.get("LAMPVM_CONFIG", str(runtime_config)))).expanduser()
    config = load_config(runtime_config, config_path)
    name = valid_name(args.name or os.environ.get("LAMPVM_NAME") or config.get("vm", "lampvm"))
    clean = valid_name(args.clean or config.get("clean", name + "-clean"))
    if clean == name:
        raise RuntimeError("Working and clean VM names must differ")
    config.update(vm=name, clean=clean, image=args.image or config.get("image", IMAGE))
    config.setdefault("cpu", 4)
    config.setdefault("memory", 8192)
    config.setdefault("display", "1440x900")
    validate_config(config)
    if args.wait_slot < 0:
        raise RuntimeError("--wait-slot must be nonnegative")
    STATE.mkdir(parents=True, exist_ok=True)
    build_env = os.environ.copy()
    build_env["LAMPVM_STATE_DIR"] = str(STATE)
    if not args.no_build:
        run([ROOT / "bin/build"], env=build_env)
    elif not (STATE / "bin/vmctl").is_file():
        raise RuntimeError("The guest helper is not built; run lampvm build")
    with contextlib.ExitStack() as locks:
        lock_names = {name, clean}
        if args.source:
            lock_names.add(valid_name(args.source))
        for locked_name in sorted(lock_names):
            locks.enter_context(lock(locked_name + ".lock"))
        tart = tart_binary(args.no_download)
        if capture([tart, "--version"]) != VERSION:
            raise RuntimeError("This setup is validated with Tart 2.32.1; use that version")
        vm = VM(tart, name)
        marker = STATE / "vms" / (name + ".json")
        check_owned(vm, name, marker)
        check_owned(vm, clean, STATE / "vms" / (clean + ".json"), clean_source=name)
        if vm.running(name) or vm.running(clean):
            raise RuntimeError("Setup requires its working and clean VMs stopped; it will not interrupt a session")
        source = args.source or config["image"]
        if args.source and (not vm.exists(source) or vm.running(source)):
            raise RuntimeError("--from must name an existing stopped local VM")
        if args.source == name:
            raise RuntimeError("--from must differ from the destination VM")
        fresh = not vm.exists(name)
        if fresh:
            print("Cloning " + source + " into " + name + " (first image download is about 30 GB)", flush=True)
            vm.command("clone", source, name)
            atomic_json(marker, dict(name=name, clean=clean, source=source, phase="cloned"))
        configure_vm(vm, config, fresh=fresh)
        # Save config before starting so failed setup can be inspected and resumed.
        if not args.keep_config:
            atomic_json(runtime_config, config)
        try:
            vm.start(args.wait_slot)
            # A public/source image can carry historical reports. Preserve them and
            # compare later reports, while also checking any visible restart alert.
            known_panics = panic_reports(vm)
            for _ in range(60):
                dock = subprocess.run([str(tart), "exec", name, "/usr/bin/pgrep", "-q", "Dock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if dock.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Guest desktop did not log in; use a public base image with auto-login")
            guest_home = vm.text("/bin/zsh", "-c", 'print -r -- "$HOME"')
            ctl = guest_home + "/.local/bin/vmctl"
            vm.guest("/bin/mkdir", "-p", guest_home + "/.local/bin")
            with (STATE / "bin/vmctl").open("rb") as binary:
                vm.command("exec", "-i", name, "/bin/zsh", "-c", "cat > " + shlex.quote(ctl) + "; chmod 755 " + shlex.quote(ctl), stdin=binary)
            check_guest_health(vm, ctl, known_panics)
            vm.script(PROVISION)
            vm.guest(ctl, "display", *config["display"].split("x"))
            # Reboot to apply guest grants, then complete actual assistant pages.
            vm.stop()
            vm.start(args.wait_slot)
            trusted = vm.text(ctl, "trusted")
            if "true" not in trusted.lower() and "yes" not in trusted.lower():
                raise RuntimeError("Guest Accessibility validation failed: " + trusted)
            complete_desktop(tart, name, ctl)
            check_guest_health(vm, ctl, known_panics)
            vm.guest("/usr/bin/open", "-a", "TextEdit")
            time.sleep(3)
            vm.guest(ctl, "tree", "TextEdit")
            shot = "/tmp/lampvm-setup-" + str(os.getpid()) + ".png"
            vm.guest("/usr/sbin/screencapture", "-x", shot, timeout=30)
            if int(vm.text("/usr/bin/stat", "-f", "%z", shot)) < 1000:
                raise RuntimeError("Guest screenshot was empty")
            vm.guest("/bin/rm", "-f", shot)
            # Avoid AppleEvents consent for this temporary setup probe.
            vm.guest("/usr/bin/pkill", "-x", "TextEdit")
            check_guest_health(vm, ctl, known_panics)
            vm.stop()
            if vm.exists(clean):
                vm.command("delete", clean)
            vm.command("clone", name, clean)
            atomic_json(STATE / "vms" / (clean + ".json"), dict(name=clean, source=name, role="clean", panic_reports=sorted(known_panics)))
            atomic_json(marker, dict(name=name, clean=clean, source=source, phase="ready", guest_home=guest_home, panic_reports=sorted(known_panics)))
            print("Setup ready: " + name + " and " + clean + " are stopped. Run lampvm start. Config: " + str(runtime_config))
        finally:
            # The only VM touched here is the owned destination. Keep it for resumable setup.
            vm.stop()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print("lampvm setup: " + str(error), file=sys.stderr)
        sys.exit(1)
