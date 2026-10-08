"""Durable slot tickets and observations. Silence never authorizes stopping a VM."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

RETAIN = 3600  # Seconds a finished request and its leftover files stay inspectable.
OVER = ('stopped', 'cancelled', 'expired')


def flock_file(path):
    """Open and exclusively lock path without waiting; None while another process holds it.

    The inode check means a lock file removed by prune is never mistaken for
    the live one: every holder owns the file currently at that path.
    """
    while True:
        handle = path.open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(handle.fileno()).st_ino == os.stat(path).st_ino:
                return handle
        except BlockingIOError:
            handle.close()
            return None
        except FileNotFoundError:
            pass
        handle.close()


class Slots:
    def __init__(self, state):
        self.state = Path(state)
        self.path = self.state / 'slots.json'

    @contextlib.contextmanager
    def edit(self, write=True):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'slots.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {'requests': {}, 'holders': {}}
            yield data
            if not write:
                return
            temp = self.path.with_name('slots-' + uuid.uuid4().hex + '.tmp')
            try:
                with temp.open('x') as stream:
                    json.dump(data, stream, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(self.path)
            finally:
                temp.unlink(missing_ok=True)

    def snapshot(self):
        with self.edit(write=False) as data:
            return json.loads(json.dumps(data))

    @staticmethod
    def waiting(data, now):
        return sorted((r for r in data['requests'].values()
                       if r['phase'] == 'queued' and r['expires_at'] > now),
                      key=lambda r: (r['sequence'], r['id']))

    def enqueue(self, name, seconds, owner, config, tart, idle_after, session=None):
        now = time.time()
        with self.edit() as data:
            old = data['requests'].get(name)
            if old and (old['phase'] == 'booting' or
                        old['phase'] == 'queued' and old['expires_at'] > now):
                return dict(old)  # Retries retain both original place and deadline.
            data['next_sequence'] = data.get('next_sequence', 0) + 1
            request = dict(sequence=data['next_sequence'], id=uuid.uuid4().hex, name=name, owner=owner,
                           session=session,
                           enqueued_at=now, expires_at=now + seconds,
                           phase='queued', config=config, tart=str(tart),
                           idle_after=idle_after, heartbeat=None)
            data['requests'][name] = request
            return dict(request)

    def update_request(self, name, ident, **updates):
        with self.edit() as data:
            request = data['requests'].get(name)
            if not request or request['id'] != ident:
                return False
            request.update(updates)
            return True

    def cancel(self, name):
        with self.edit() as data:
            request = data['requests'].get(name)
            if request and request['phase'] == 'queued':
                request.update(phase='cancelled', finished_at=time.time())

    def started(self, name, request):
        now = time.time()
        with self.edit() as data:
            # Preserve activity if recovering a worker for an already running VM.
            holder = data['holders'].setdefault(name, {})
            holder.setdefault('started_at', now)
            holder.setdefault('last_used_at', now)
            holder.update(owner=request['owner'], idle_after=request['idle_after'], session=request.get('session'))

    def stopped(self, name, phases=('booting', 'running', 'failed')):
        """The caller saw this VM stopped under its lifecycle lock; close its ticket too.

        Closing here does not depend on a worker surviving to notice the stop.
        A worker, which holds no lifecycle lock, passes the phases it may close:
        a reset keeping the slot shows as booting while its VM is down. False
        means the ticket was left alone.
        """
        with self.edit() as data:
            request = data['requests'].get(name)
            if request and request['phase'] in {'booting', 'running', 'failed'} - set(phases):
                return False
            data['holders'].pop(name, None)
            if request and request['phase'] in phases:
                if request['phase'] != 'failed':
                    request['phase'] = 'stopped'
                request.setdefault('finished_at', time.time())
            return True

    def restarted(self, name, ident):
        """A reset rebooted this VM without giving up its slot: same ticket, same tenure."""
        now = time.time()
        with self.edit() as data:
            request = data['requests'].get(name)
            if not request or request['id'] != ident:
                return False
            request.update(phase='running', heartbeat=now)
            for key in ('finished_at', 'error'):
                request.pop(key, None)
            holder = data['holders'].setdefault(name, {})
            holder.setdefault('started_at', now)
            holder.update(owner=request['owner'], idle_after=request['idle_after'], session=request.get('session'),
                          last_used_at=now, resets=holder.get('resets', 0) + 1)
            return True

    def worker_gone(self, name):
        """Return the held worker lock if no worker lives for name, else None."""
        (self.state / 'locks').mkdir(parents=True, exist_ok=True)
        return flock_file(self.state / 'locks' / (name + '-worker.lock'))

    def reap(self, machines):
        """Close tickets whose worker died and whose VM Tart no longer runs.

        machines is called for a fresh Tart listing while the dead worker's lock
        is held, so no recovering worker can boot that VM in between.
        """
        now = time.time()
        running = lambda: {m['Name'] for m in machines() if m.get('Source') == 'local' and m.get('Running')}
        seen = running()
        for name, request in self.snapshot()['requests'].items():
            expired = request['phase'] == 'queued' and request['expires_at'] <= now
            ended = request['phase'] in ('running', 'failed') and 'finished_at' not in request and name not in seen
            if not expired and not ended:
                continue
            handle = self.worker_gone(name)
            if not handle:
                continue  # Its live worker records the outcome itself.
            with handle:
                if ended and name in running():
                    continue
                with self.edit() as data:
                    current = data['requests'].get(name)
                    if not current or current['id'] != request['id'] or current['phase'] != request['phase']:
                        continue
                    if ended:
                        data['holders'].pop(name, None)
                    if current['phase'] != 'failed':  # A failure stays readable until pruned.
                        current['phase'] = 'expired' if expired else 'stopped'
                    current['finished_at'] = now
                    current['reaped'] = 'worker gone; ' + ('waiting deadline passed' if expired else 'VM not running')

    def prune(self, machines, retain=RETAIN):
        """Forget finished requests after retain seconds, then files of VMs that no longer exist."""
        now = time.time()
        with self.edit() as data:
            for name, request in list(data['requests'].items()):
                if request['phase'] not in OVER and not (request['phase'] == 'failed' and 'finished_at' in request):
                    continue
                # Records from before finished_at existed carry only these times.
                finished = request.get('finished_at') or max(request.get('heartbeat') or 0, request['enqueued_at'])
                if now - finished >= retain and name not in data['holders']:
                    del data['requests'][name]
            known = set(data['requests']) | set(data['holders'])
        known |= {m['Name'] for m in machines()}
        if (self.state / 'vms').is_dir():
            known |= {p.stem for p in (self.state / 'vms').glob('*.json')}
        old = lambda path: now - path.stat().st_mtime >= retain
        gone = lambda name: name not in known and name != 'host-start'
        removed = []
        for path in sorted((self.state / 'logs').glob('*.log')):
            match = re.fullmatch(r'(.+?)-(?:slot-)?[0-9a-f]{32}\.log', path.name)
            if match and gone(match.group(1)) and old(path):
                path.unlink(missing_ok=True)
                removed.append(path.name)
        for path in sorted((self.state / 'locks').glob('*.lock')):
            name = path.name[:-len('.lock')]
            # NAME-worker.lock is NAME's worker lock or the lifecycle lock of a VM so named.
            if not gone(name) or name.endswith('-worker') and not gone(name[:-len('-worker')]) or not old(path):
                continue
            handle = flock_file(path)
            if handle:
                with handle:  # Unlinked while held: a waiter rechecks the inode and reopens.
                    path.unlink(missing_ok=True)
                removed.append(path.name)
        return removed

    @contextlib.contextmanager
    def activity(self, name, command):
        ident = uuid.uuid4().hex
        now = time.time()
        with self.edit() as data:
            holder = data['holders'].setdefault(name, {})
            holder.update(last_used_at=now, last_command=command)
            holder.setdefault('active', {})[ident] = dict(pid=os.getpid(), command=command, since=now)
        try:
            yield
        finally:
            with self.edit() as data:
                holder = data['holders'].get(name)
                if holder:
                    holder.setdefault('active', {}).pop(ident, None)
                    holder['last_used_at'] = time.time()

    def hold(self, name, seconds, reason):
        with self.edit() as data:
            holder = data['holders'].setdefault(name, {})
            holder['hold'] = dict(until=time.time() + seconds, reason=reason) if seconds else None

    @staticmethod
    def observation(holder, now):
        if not holder or 'started_at' not in holder:
            return dict(attention='untracked; activity unknown', idle_seconds=None)
        idle = max(0, now - holder['last_used_at'])
        active = []
        for operation in holder.get('active', {}).values():
            try:
                os.kill(operation['pid'], 0)
                active.append(operation)
            except ProcessLookupError:
                pass
            except PermissionError:
                active.append(operation)
        hold = holder.get('hold')
        if active:
            attention = 'command running'
        elif hold and hold['until'] > now:
            attention = 'held: ' + hold['reason']
        elif idle >= holder.get('idle_after', 600):
            attention = 'idle attention; guest work unknown'
        else:
            attention = 'recently used'
        return dict(attention=attention, idle_seconds=round(idle), active=active)

    def report(self, machines):
        now = time.time()
        data = self.snapshot()
        holders = []
        for machine in machines:
            if machine.get('Source') != 'local' or not machine.get('Running'):
                continue
            name = machine['Name']
            holder = data['holders'].get(name, {})
            request = data['requests'].get(name, {})
            heartbeat = request.get('heartbeat')
            limit = 240 if request.get('phase') == 'booting' else 30
            monitor = ('responsive' if heartbeat and now-heartbeat < limit else
                       'stalled; rerun start to resume') if request else 'untracked'
            # Tickets saved before sessions existed, and untracked VMs, have none.
            holders.append({'name': name, **holder, **self.observation(holder, now), 'monitor': monitor,
                            'session': holder.get('session') or request.get('session')})
        queue = []
        for position, request in enumerate(self.waiting(data, now), 1):
            heartbeat = request.get('heartbeat')
            queue.append(dict(name=request['name'], owner=request['owner'], session=request.get('session'), position=position,
                              enqueued_at=request['enqueued_at'], waited_seconds=round(now-request['enqueued_at']),
                              expires_at=request['expires_at'], remaining_seconds=round(request['expires_at']-now),
                              worker='responsive' if heartbeat and now-heartbeat < 30 else 'stalled; rerun start to resume',
                              log=request.get('log')))
        requests = [{k: v for k, v in r.items() if k not in ('config', 'tart')}
                    for r in data['requests'].values()]
        groups = {}
        for kind, entries in (('running', holders), ('queued', queue)):
            for entry in entries:
                group = groups.setdefault(entry['session'], dict(session=entry['session'], running=[], queued=[]))
                group[kind].append(entry['name'])
        sessions = sorted(groups.values(), key=lambda group: (group['session'] is None, group['session'] or ''))
        return dict(limit=2, holders=holders, queue=queue, sessions=sessions, requests=requests,
                    estimate='unknown: guest jobs have no predictable completion time')
