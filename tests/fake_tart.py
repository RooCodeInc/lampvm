#!/usr/bin/env python3
"""Process-level scheduler stand-in; requires explicitly isolated fake state."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import sys
import time

path = Path(os.environ['FAKE_TART_STATE'])

@contextlib.contextmanager
def edit():
    with path.with_suffix('.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = json.loads(path.read_text())
        yield data
        path.write_text(json.dumps(data))

args = sys.argv[1:]
command = args[0]
if command == 'list':
    with edit() as data:
        print(json.dumps([dict(Name=name, Source='local', Running=running) for name, running in data['vms'].items()]))
elif command == 'run':
    name = args[-1]
    with edit() as data:
        if sum(data['vms'].values()) >= 2:
            sys.exit(2)
        data['vms'][name] = True
        data.setdefault('starts', []).append(name)
        data.setdefault('run_pids', []).append(os.getpid())
    while True:
        with edit() as data:
            running = data['vms'].get(name)
        if not running:
            break
        time.sleep(.1)
elif command == 'exec':
    name = args[1]
    guest = args[2:]
    with edit() as data:
        if not data['vms'].get(name):
            sys.exit(3)
    if '/sbin/shutdown' in guest:
        with edit() as data:
            data['vms'][name] = False
            data.setdefault('stops', []).append(name)
    elif guest[0] == '/bin/sleep':
        time.sleep(float(guest[1]))
    elif guest[0] == '/bin/zsh':
        print('/guest/vmctl')
    elif guest[0] == '/guest/vmctl':
        print('[]' if guest[1] in ('tree', 'windows') else 'true')
elif command == 'delete':
    with edit() as data:
        del data['vms'][args[1]]
elif command == 'clone':
    with edit() as data:
        if data.get('fail_clone'):
            sys.exit(4)
        data['vms'][args[2]] = False
elif command == 'stop':
    with edit() as data:
        data['vms'][args[1]] = False
        data.setdefault('stops', []).append(args[1])
else:
    sys.exit('unsupported fake Tart command')
