"""Durable slot tickets and observations. Silence never authorizes stopping a VM."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time
import uuid


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

    def enqueue(self, name, seconds, owner, config, tart, idle_after):
        now = time.time()
        with self.edit() as data:
            old = data['requests'].get(name)
            if old and (old['phase'] == 'booting' or
                        old['phase'] == 'queued' and old['expires_at'] > now):
                return dict(old)  # Retries retain both original place and deadline.
            data['next_sequence'] = data.get('next_sequence', 0) + 1
            request = dict(sequence=data['next_sequence'], id=uuid.uuid4().hex, name=name, owner=owner,
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
                request['phase'] = 'cancelled'

    def started(self, name, request):
        now = time.time()
        with self.edit() as data:
            # Preserve activity if recovering a worker for an already running VM.
            holder = data['holders'].setdefault(name, {})
            holder.setdefault('started_at', now)
            holder.setdefault('last_used_at', now)
            holder.update(owner=request['owner'], idle_after=request['idle_after'])

    def stopped(self, name):
        with self.edit() as data:
            data['holders'].pop(name, None)

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
            holders.append({'name': name, **holder, **self.observation(holder, now), 'monitor': monitor})
        queue = []
        for position, request in enumerate(self.waiting(data, now), 1):
            heartbeat = request.get('heartbeat')
            queue.append(dict(name=request['name'], owner=request['owner'], position=position,
                              enqueued_at=request['enqueued_at'], waited_seconds=round(now-request['enqueued_at']),
                              expires_at=request['expires_at'], remaining_seconds=round(request['expires_at']-now),
                              worker='responsive' if heartbeat and now-heartbeat < 30 else 'stalled; rerun start to resume',
                              log=request.get('log')))
        requests = [{k: v for k, v in r.items() if k not in ('config', 'tart')}
                    for r in data['requests'].values()]
        return dict(limit=2, holders=holders, queue=queue, requests=requests,
                    estimate='unknown: guest jobs have no predictable completion time')
