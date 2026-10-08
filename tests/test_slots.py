"""Durability/fairness and process-loss proof without occupying macOS VM slots."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from slots import Slots, flock_file
ROOT = Path(__file__).resolve().parents[1]


class SlotContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lampvm-slots-')
        self.addCleanup(temp.cleanup)
        self.slots = Slots(temp.name)

    def enqueue(self, name, seconds=100):
        return self.slots.enqueue(name, seconds, name, {}, '/fake', 600)

    def test_dead_caller_retry_keeps_original_place_and_deadline(self):
        with mock.patch('slots.time.time', return_value=100):
            first = self.enqueue('one')
        with mock.patch('slots.time.time', return_value=120):
            self.enqueue('two')
            retry = self.enqueue('one', 9999)
        self.assertEqual(first, retry)
        self.assertEqual([r['name'] for r in self.slots.waiting(self.slots.snapshot(), 130)], ['one', 'two'])
        self.assertEqual(retry['expires_at'], 200)

    def test_fifo_uses_sequence_even_when_wall_clock_changes(self):
        with mock.patch('slots.time.time', return_value=150):
            self.enqueue('first')
        with mock.patch('slots.time.time', return_value=100):
            self.enqueue('second')
        self.assertEqual([r['name'] for r in self.slots.waiting(self.slots.snapshot(), 160)], ['first', 'second'])

    def test_expiry_and_cancel_remove_only_their_own_place(self):
        with mock.patch('slots.time.time', return_value=100):
            self.enqueue('expired', 1)
            self.enqueue('cancelled')
            self.enqueue('next')
        self.slots.cancel('cancelled')
        self.assertEqual([r['name'] for r in self.slots.waiting(self.slots.snapshot(), 102)], ['next'])

    def test_long_active_command_and_timed_hold_suppress_idle_flag(self):
        holder = dict(started_at=100, last_used_at=100, idle_after=600,
                      active={'token': dict(pid=os.getpid(), command='exec', since=100)})
        self.assertEqual(self.slots.observation(holder, 2500)['attention'], 'command running')
        holder['active'] = {}
        holder['hold'] = dict(until=3700, reason='40-minute guest scenario')
        self.assertEqual(self.slots.observation(holder, 2500)['attention'], 'held: 40-minute guest scenario')
        self.assertIn('idle attention', self.slots.observation(holder, 3800)['attention'])

    def test_dead_command_does_not_protect_abandoned_holder(self):
        holder = dict(started_at=100, last_used_at=100, idle_after=600,
                      active={'token': dict(pid=1234, command='exec', since=100)})
        with mock.patch('slots.os.kill', side_effect=ProcessLookupError):
            self.assertIn('idle attention', self.slots.observation(holder, 2800)['attention'])

    def test_external_holders_are_visible_with_unknown_activity(self):
        report = self.slots.report([dict(Name='someone-else', Source='local', Running=True)])
        self.assertEqual(report['holders'][0]['attention'], 'untracked; activity unknown')
        self.assertIsNone(report['holders'][0]['idle_seconds'])

    def test_finished_and_failed_command_refresh_activity_and_remove_active(self):
        request = self.enqueue('private')
        self.slots.started('private', request)
        with self.assertRaises(RuntimeError):
            with self.slots.activity('private', 'exec'):
                self.assertEqual(len(self.slots.snapshot()['holders']['private']['active']), 1)
                raise RuntimeError('failed')
        self.assertEqual(self.slots.snapshot()['holders']['private']['active'], {})

    def running(self, name, **fields):
        request = self.enqueue(name)
        self.slots.started(name, request)
        self.slots.update_request(name, request['id'], phase='running', **fields)
        return request

    def phase(self, name):
        return self.slots.snapshot()['requests'][name]['phase']

    def test_stop_closes_the_ticket_without_a_worker(self):
        self.running('private')
        self.slots.stopped('private')
        data = self.slots.snapshot()
        self.assertEqual(data['requests']['private']['phase'], 'stopped')
        self.assertIn('finished_at', data['requests']['private'])
        self.assertEqual(data['holders'], {})

    def test_reap_closes_running_ticket_only_when_worker_and_vm_are_both_gone(self):
        for name in ('dead', 'alive-vm', 'alive-worker'):
            self.running(name)
        machines = lambda: [dict(Name='alive-vm', Source='local', Running=True)]
        (self.slots.state / 'locks').mkdir()
        with flock_file(self.slots.state / 'locks/alive-worker-worker.lock'):
            self.slots.reap(machines)
        data = self.slots.snapshot()
        self.assertEqual(data['requests']['dead']['phase'], 'stopped')
        self.assertIn('worker gone', data['requests']['dead']['reaped'])
        self.assertEqual(sorted(data['holders']), ['alive-vm', 'alive-worker'])
        self.assertEqual(self.phase('alive-vm'), 'running')
        self.assertEqual(self.phase('alive-worker'), 'running')

    def test_reap_rechecks_tart_after_taking_the_worker_lock(self):
        self.running('late')
        listings = iter([[], [dict(Name='late', Source='local', Running=True)]])
        self.slots.reap(lambda: next(listings))
        self.assertEqual(self.phase('late'), 'running')

    def test_reap_expires_dead_waiter_and_keeps_failure_readable(self):
        with mock.patch('slots.time.time', return_value=100):
            self.enqueue('late', 1)
            self.enqueue('patient', 10 ** 10)
        failed = self.enqueue('broken')
        self.slots.update_request('broken', failed['id'], phase='failed', error='Boot timed out')
        self.slots.reap(lambda: [])
        data = self.slots.snapshot()['requests']
        self.assertEqual(data['late']['phase'], 'expired')
        self.assertEqual(data['patient']['phase'], 'queued')
        self.assertEqual((data['broken']['phase'], data['broken']['error']), ('failed', 'Boot timed out'))
        self.assertIn('finished_at', data['broken'])

    def test_prune_forgets_only_old_finished_requests_and_files_of_vanished_vms(self):
        state = self.slots.state
        for folder in ('logs', 'locks', 'vms'):
            (state / folder).mkdir()
        ident = 'a' * 32
        names = ('gone', 'recent', 'stopped-vm', 'marked', 'queued', 'holder', 'busy')
        for name in names:
            for file in ('logs/%s-slot-%s.log' % (name, ident), 'logs/%s-%s.log' % (name, ident),
                         'locks/%s.lock' % name, 'locks/%s-worker.lock' % name):
                (state / file).touch()
                if name != 'recent':
                    os.utime(state / file, (1000, 1000))
        (state / 'locks/host-start.lock').touch()
        os.utime(state / 'locks/host-start.lock', (1000, 1000))
        (state / 'logs/notes.log').touch()
        os.utime(state / 'logs/notes.log', (1000, 1000))
        (state / 'vms/marked.json').write_text('{}')
        with mock.patch('slots.time.time', return_value=1500):
            for name in ('gone', 'stopped-vm', 'marked', 'busy'):  # Finished long ago, before finished_at existed.
                self.slots.update_request(name, self.enqueue(name)['id'], phase='stopped', heartbeat=2000)
        self.slots.cancel(self.enqueue('recent')['name'])
        self.enqueue('queued')
        self.running('holder')
        with flock_file(state / 'locks/busy.lock'):
            removed = self.slots.prune(lambda: [dict(Name='stopped-vm', Source='local', Running=False)])
        self.assertEqual(sorted(self.slots.snapshot()['requests']), ['holder', 'queued', 'recent'])
        left = sorted(p.name for folder in ('logs', 'locks') for p in (state / folder).iterdir())
        expected = ['host-start.lock', 'notes.log', 'busy.lock']
        for name in ('recent', 'stopped-vm', 'marked', 'queued', 'holder'):
            expected += ['%s-slot-%s.log' % (name, ident), '%s-%s.log' % (name, ident), name + '.lock', name + '-worker.lock']
        self.assertEqual(left, sorted(expected))
        self.assertEqual(len(removed), 7)

    def test_lock_removed_by_prune_is_never_held_as_a_stale_file(self):
        path = self.slots.state / 'locks/private.lock'
        path.parent.mkdir()
        first = flock_file(path)
        path.unlink()  # What prune does while holding it.
        second = flock_file(path)  # A new file: the old holder does not block it.
        self.assertEqual(os.fstat(second.fileno()).st_ino, path.stat().st_ino)
        self.assertIsNone(flock_file(path))
        first.close()
        second.close()


class DetachedProcessProof(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lampvm-process-proof-')
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.fake = self.state / 'fake.json'
        self.fake.write_text(json.dumps(dict(vms={'external-a': True, 'external-b': True, 'one': False, 'two': False})))
        (self.state / 'config.json').write_text('{}')
        (self.state / 'vms').mkdir()
        for name in ('one', 'two'):
            (self.state / 'vms' / (name+'.json')).write_text(json.dumps(dict(name=name)))
        self.env = {**os.environ, 'LAMPVM_STATE_DIR': str(self.state), 'LAMPVM_CONFIG': str(self.state/'config.json'),
                    'LAMPVM_TART': str(ROOT/'tests/fake_tart.py'), 'FAKE_TART_STATE': str(self.fake), 'LAMPVM_APP': ''}
        self.slots = Slots(self.state)
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # All PIDs come exclusively from this isolated fake test state.
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        data = self.slots.snapshot()
        pids = [r.get('worker_pid') for r in data['requests'].values()]
        with self.fake.with_suffix('.lock').open('a') as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            pids += json.loads(self.fake.read_text()).get('run_pids', [])
        for pid in filter(None, pids):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def cli(self, name, *args):
        return subprocess.run([str(ROOT/'bin/lampvm'), '--name', name, *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def until(self, predicate, timeout=20):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        self.fail('condition timed out; ' + json.dumps(self.slots.snapshot()))

    def request(self, name):
        return self.slots.snapshot()['requests'].get(name, {})

    def free_external_slot(self):
        import fcntl
        with self.fake.with_suffix('.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            data = json.loads(self.fake.read_text())
            data['vms']['external-a'] = False
            self.fake.write_text(json.dumps(data))

    def test_killed_waiter_survives_fifo_stale_worker_recovery_and_idle_safety(self):
        # Kill the foreground waiter just as the agent host would.
        process = subprocess.Popen([str(ROOT/'bin/lampvm'), '--name', 'one', 'start', '--wait', '90',
                                    '--owner', 'session-one', '--idle-after', '1'], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(process)
        self.until(lambda: self.request('one').get('worker_pid'))
        original = self.request('one')
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        result = self.cli('two', 'start', '--wait', '90', '--detach')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.until(lambda: self.request('two').get('worker_pid'))
        self.assertEqual([r['name'] for r in self.slots.waiting(self.slots.snapshot(), time.time())], ['one', 'two'])
        second_id = self.request('two')['id']
        self.assertEqual(self.cli('two', 'start', '--detach').returncode, 0)
        self.assertEqual(self.request('two')['id'], second_id)
        # Even loss of the detached worker is recoverable without losing place.
        os.kill(original['worker_pid'], signal.SIGKILL)
        self.until(lambda: time.time()-self.request('one')['heartbeat'] > 2)
        resumed = self.cli('one', 'start', '--wait', '3600', '--detach')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.request('one')['id'], original['id'])
        self.assertEqual(self.request('one')['expires_at'], original['expires_at'])
        self.until(lambda: self.request('one').get('worker_pid') != original['worker_pid'])
        self.free_external_slot()
        self.until(lambda: self.request('one').get('phase') == 'running')
        self.assertEqual(self.request('two')['phase'], 'queued')
        self.until(lambda: 'idle attention' in self.slots.snapshot()['holders']['one'].get('attention', ''))
        result = self.cli('one', 'hold', '--seconds', '3600', '--reason', '40-minute guest scenario')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.until(lambda: self.slots.snapshot()['holders']['one'].get('attention', '').startswith('held:'))
        # A live guest command is protected for its whole execution, regardless
        # of time since the previous lampvm call.
        self.assertEqual(self.cli('one', 'release-hold').returncode, 0)
        job = subprocess.Popen([str(ROOT/'bin/lampvm'), '--name', 'one', 'exec', '/bin/sleep', '5'],
                               env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(job)
        self.until(lambda: self.slots.snapshot()['holders']['one'].get('attention') == 'command running')
        report = json.loads(self.cli('one', 'status', '--json').stdout)
        self.assertEqual(next(h['attention'] for h in report['holders'] if h['name'] == 'one'), 'command running')
        job.wait(timeout=10)
        self.assertEqual(job.returncode, 0)
        self.assertEqual(self.cli('one', 'hold', '--seconds', '3600', '--reason', '40-minute guest scenario').returncode, 0)
        report = json.loads(self.cli('one', 'status', '--json').stdout)
        proof = os.environ.get('LAMPVM_PROOF_DIR')
        if proof:
            destination = Path(proof)
            destination.mkdir(parents=True, exist_ok=True)
            (destination/'fake-slots-status.json').write_text(json.dumps(report, indent=2)+'\n')
            (destination/'fake-slots-status.txt').write_text(self.cli('one', 'status').stdout)
            (destination/'fake-worker.log').write_text(Path(self.request('one')['log']).read_text())
        self.assertEqual(report['queue'][0]['name'], 'two')
        self.assertEqual(len(report['holders']), 2)
        self.assertEqual(json.loads(self.fake.read_text()).get('stops', []), [])
        running_worker = self.request('one')['worker_pid']
        started_at = self.slots.snapshot()['holders']['one']['started_at']
        os.kill(running_worker, signal.SIGKILL)
        self.slots.update_request('one', original['id'], heartbeat=time.time()-31)
        stalled = json.loads(self.cli('one', 'status', '--json').stdout)
        self.assertIn('stalled', next(h['monitor'] for h in stalled['holders'] if h['name'] == 'one'))
        self.assertEqual(self.cli('one', 'start', '--detach').returncode, 0)
        self.until(lambda: self.request('one').get('worker_pid') != running_worker)
        self.assertEqual(self.slots.snapshot()['holders']['one']['started_at'], started_at)
        self.assertEqual(self.cli('one', 'stop').returncode, 0)
        self.until(lambda: self.request('two').get('phase') == 'running')
        self.assertEqual(json.loads(self.fake.read_text())['starts'], ['one', 'two'])
        self.assertEqual(self.cli('two', 'stop').returncode, 0)

    def kill_worker_and_vm(self, name):
        # What `pkill -f "lampvm.*NAME"` did to request 29: worker and VM die together.
        self.free_external_slot()
        self.assertEqual(self.cli(name, 'start', '--wait', '60', '--detach').returncode, 0)
        self.until(lambda: self.request(name).get('phase') == 'running')
        os.kill(self.request(name)['worker_pid'], signal.SIGKILL)
        import fcntl
        with self.fake.with_suffix('.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            data = json.loads(self.fake.read_text())
            data['vms'][name] = False
            self.fake.write_text(json.dumps(data))
        self.assertEqual(self.request(name)['phase'], 'running')

    def test_stop_after_worker_and_vm_were_killed_closes_the_ticket(self):
        self.kill_worker_and_vm('one')
        result = self.cli('one', 'stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.request('one')['phase'], 'stopped')
        self.assertNotIn('one', self.slots.snapshot()['holders'])

    def test_status_reaps_ticket_whose_worker_and_vm_were_killed(self):
        self.kill_worker_and_vm('one')
        report = json.loads(self.cli('two', 'status', '--json').stdout)
        ticket = next(r for r in report['requests'] if r['name'] == 'one')
        self.assertEqual(ticket['phase'], 'stopped')
        self.assertIn('worker gone', ticket['reaped'])
        self.assertNotIn('one', self.slots.snapshot()['holders'])
        self.assertEqual(json.loads(self.fake.read_text()).get('stops', []), [])

    def test_expired_or_cancelled_ticket_never_starts(self):
        import fcntl
        (self.state/'locks').mkdir(exist_ok=True)
        # Another long boot owns host-start.lock. Saving a wait must not block
        # behind that boot, and expiry must include all elapsed waiting time.
        with (self.state/'locks/host-start.lock').open('a') as host_lock:
            fcntl.flock(host_lock, fcntl.LOCK_EX)
            self.assertEqual(self.cli('one', 'start', '--wait', '1', '--detach').returncode, 0)
            self.until(lambda: self.request('one').get('phase') == 'expired')
        self.assertEqual(self.cli('two', 'start', '--wait', '60', '--detach').returncode, 0)
        self.assertEqual(self.cli('two', 'cancel-start').returncode, 0)
        self.free_external_slot()
        time.sleep(2.5)
        self.assertEqual(json.loads(self.fake.read_text()).get('starts', []), [])


if __name__ == '__main__':
    unittest.main()
