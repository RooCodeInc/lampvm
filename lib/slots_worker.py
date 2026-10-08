#!/usr/bin/env python3
"""One detached worker per private VM; no idle observation ever stops a guest."""
import argparse
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('lampvm_worker_cli', str(ROOT / 'bin/lampvm'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


def attempt_start(vm, slots, ident):
    # Same lock order as lifecycle commands. No long lock wait hides worker
    # health or prevents cancellation of a queued VM.
    try:
        with cli.lock(vm.state / 'locks' / (vm.name + '.lock'), timeout=0):
            with cli.lock(vm.state / 'locks/host-start.lock', timeout=0):
                data = slots.snapshot()
                current = data['requests'].get(vm.name)
                if not current or current['id'] != ident or current['phase'] not in ('queued', 'booting'):
                    return
                queue = slots.waiting(data, time.time())
                if current['phase'] == 'queued' and (not queue or queue[0]['id'] != ident):
                    return
                machines = vm.machines()
                running = any(v['Name'] == vm.name and v.get('Running') and v.get('Source') == 'local' for v in machines)
                if not running and sum(bool(v.get('Running')) for v in machines if v.get('Source') == 'local') >= 2:
                    return
                slots.update_request(vm.name, ident, phase='booting', heartbeat=time.time())
                with slots.activity(vm.name, 'boot'):
                    vm.boot(lambda: slots.started(vm.name, current))
                slots.update_request(vm.name, ident, phase='running', heartbeat=time.time())
                print('running: ' + vm.name, flush=True)
    except cli.Error as error:
        if str(error) != 'VM is busy; lock wait timed out':
            raise


def work(state, name, ident):
    slots = cli.Slots(state)
    worker_lock = state / 'locks' / (cli.valid_name(name) + '-worker.lock')
    worker_lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10
    while True:
        handle = cli.flock_file(worker_lock)
        if handle:
            break
        try:
            holder = worker_lock.read_text().strip()
        except FileNotFoundError:
            holder = ''
        if holder == ident or time.monotonic() > deadline:
            return  # An existing worker already handles this ticket.
        time.sleep(.1)  # Previous cancelled/stopped ticket is exiting.
    with handle:
        handle.seek(0)
        handle.truncate()
        handle.write(ident)
        handle.flush()
        request = slots.snapshot()['requests'].get(name)
        if not request or request['id'] != ident:
            return
        # Captured effective config is authoritative, not today's caller's env.
        os.environ.pop('LAMPVM_CONFIG', None)
        vm = cli.VM(argparse.Namespace(state_dir=str(state), config=None, name=name, app=None))
        vm.config = request['config']
        vm.clean = cli.valid_name(vm.config.get('clean', 'lampvm-clean'))
        vm.tart = Path(request['tart'])
        previous = None
        while True:
            request = slots.snapshot()['requests'].get(name)
            if not request or request['id'] != ident:
                return
            phase = request['phase']
            if phase in ('cancelled', 'expired', 'stopped'):
                return
            slots.update_request(name, ident, heartbeat=time.time(), worker_pid=os.getpid())
            try:
                if phase in ('queued', 'booting'):
                    if phase == 'queued' and request['expires_at'] <= time.time():
                        slots.update_request(name, ident, phase='expired', finished_at=time.time())
                        print('slot request expired; original waiting deadline reached', flush=True)
                        return
                    attempt_start(vm, slots, ident)
                elif not vm.running():
                    slots.stopped(name)
                    return
                if slots.snapshot()['requests'][name]['phase'] in ('running', 'failed'):
                    holder = slots.snapshot()['holders'].get(name, {})
                    attention = slots.observation(holder, time.time())['attention']
                    if attention != previous:
                        print(time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + name + ': ' + attention, flush=True)
                        with slots.edit() as data:
                            if name in data['holders']:
                                data['holders'][name]['attention'] = attention
                                data['holders'][name]['observed_at'] = time.time()
                        previous = attention
            except Exception as error:
                slots.update_request(name, ident, phase='failed', error=str(error))
                print('start/monitor failed: ' + str(error), flush=True)
                # Keep observing a VM left running for inspection. Never stop it.
                try:
                    if not vm.running():
                        return
                except Exception:
                    return
            time.sleep(2)


if __name__ == '__main__':
    work(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
