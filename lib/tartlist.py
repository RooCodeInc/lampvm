"""List Tart's VMs without tripping over another session's delete."""
import json
import os
import subprocess
import sys
import time

ATTEMPTS = 10


def tart_list(tart):
    """Tart's VM list as parsed JSON, or the CalledProcessError of its last attempt.

    Tart 2.32.1's `tart list` first keeps the VM folders whose files exist, then
    looks those files up again for sizes and dates. When another process's `tart
    delete` removes a VM in between, the whole listing fails ("The file config.json
    couldn't be opened"). That says nothing about the VMs still there, so the
    listing is taken again.
    """
    for attempt in range(ATTEMPTS):
        result = subprocess.run([str(tart), "list", "--format", "json"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**os.environ, "TART_NO_AUTO_PRUNE": "1"})
        if result.returncode == 0:
            return json.loads(result.stdout)
        if attempt + 1 < ATTEMPTS:
            time.sleep(.2)
    sys.stderr.write(result.stderr)
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
