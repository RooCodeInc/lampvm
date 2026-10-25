"""Reads a VM spec: the file `lampvm up` turns into a VM that is ready for commands."""
import json
import math
from pathlib import Path
import re

OCI = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+|@sha256:[a-f0-9]{64})")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class SpecError(ValueError):
    pass


def keys(where, data, allowed, required=()):
    if not isinstance(data, dict):
        raise SpecError(where + " must be a JSON object")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SpecError(where + " has unknown keys: " + ", ".join(unknown))
    missing = [key for key in required if key not in data]
    if missing:
        raise SpecError(where + " needs: " + ", ".join(missing))


def text(where, value):
    if not isinstance(value, str) or not value.strip():
        raise SpecError(where + " must be a nonempty string")
    return value


def seconds(where, value, default):
    value = default if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 3600:
        raise SpecError(where + " must be 1..3600 seconds")
    return float(value)


def load(path):
    """Returns the spec with host paths made absolute against the spec's own folder."""
    path = Path(path).expanduser().resolve()
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise SpecError("Cannot read spec " + str(path) + ": " + str(error))
    except ValueError as error:
        raise SpecError("Spec " + str(path) + " is not JSON: " + str(error))
    keys("spec", data, ["image", "install", "files", "launch", "ready"], ["image"])
    keys("image", data["image"], ["name", "from", "cpu", "memory", "display"], ["name", "from"])
    image = dict(data["image"])
    if not NAME.fullmatch(text("image.name", image["name"])):
        raise SpecError("image.name must be a VM name: letters, digits, dot, underscore or hyphen, at most 80")
    source = text("image.from", image["from"])
    if not (OCI.fullmatch(source) or NAME.fullmatch(source)):
        raise SpecError("image.from must be an OCI image with a tag or @sha256 digest, or a stopped local VM")
    if source == image["name"]:
        raise SpecError("image.from must differ from image.name")
    image["oci"] = bool(OCI.fullmatch(source))
    base = path.parent

    def host(where, value):
        return str((base / Path(text(where, value)).expanduser()).resolve())

    install = data.get("install", [])
    if not isinstance(install, list):
        raise SpecError("install must be a list of .app paths")
    install = [host("install[%d]" % i, app) for i, app in enumerate(install)]
    for app in install:
        if not app.endswith(".app"):
            raise SpecError("install expects .app bundles: " + app)
    files = data.get("files", [])
    if not isinstance(files, list):
        raise SpecError("files must be a list of {from, to}")
    copies = []
    for i, item in enumerate(files):
        keys("files[%d]" % i, item, ["from", "to"], ["from", "to"])
        to = text("files[%d].to" % i, item["to"])
        if not to.startswith("/"):
            raise SpecError("files[%d].to must be an absolute guest folder" % i)
        copies.append({"from": host("files[%d].from" % i, item["from"]), "to": to})
    launch = data.get("launch")
    if launch is not None:
        text("launch", launch)
    ready = data.get("ready", [])
    if not isinstance(ready, list):
        raise SpecError("ready must be a list of checks")
    checks = []
    for i, check in enumerate(ready):
        where = "ready[%d]" % i
        if isinstance(check, dict) and "exec" in check:
            keys(where, check, ["exec", "expect", "timeout"])
            argv = check["exec"]
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or not argv[0].startswith("/"):
                raise SpecError(where + ".exec must be a command list starting with an absolute guest path")
            expect = check.get("expect")
            if expect is not None:
                text(where + ".expect", expect)
            checks.append({"exec": argv, "expect": expect, "timeout": seconds(where + ".timeout", check.get("timeout"), 60)})
        elif isinstance(check, dict) and "text" in check:
            keys(where, check, ["app", "text", "timeout"], ["app"])
            checks.append({"app": text(where + ".app", check["app"]), "text": text(where + ".text", check["text"]),
                           "timeout": seconds(where + ".timeout", check.get("timeout"), 60)})
        else:
            raise SpecError(where + " must be {\"exec\": [...]} or {\"app\": APP, \"text\": TEXT}")
    return {"path": str(path), "image": image, "install": install, "files": copies, "launch": launch, "ready": checks}
