"""Durable calendar occurrences, dispatched into existing backup and benchmark work.

Each due time is claimed atomically before HTTP. Retries reuse the same job ID/token.
Only one occurrence per schedule may be open; later overlapping occurrences are skipped.
"""

from loadcoach.log import LEGACY  # the old default node, which rows and catalogues without a node name mean
import asyncio
import json
import os
import secrets
import time

import httpx

from db import database, local_path, migrate
from db.schemas import SCHEDULES
from loadcoach.schedule_time import next_at, occurrence_ids, validate


class Schedules:
    """Durable recurring schedules and their claimed occurrence history."""
    def __init__(self, source):
        """Open durable schedule storage and apply its portable schema migrations."""
        target = database(source)
        self.db = target.connect(check_same_thread=False)
        if path := local_path(target):
            os.chmod(path, 0o600)  # scoped schedule/task tokens, never session keys
        migrate(target, self.db, 'weightroom.schedules', SCHEDULES)

    def create(self, identifier, name, owner_id, owner_name, spec, token, now=None, model_name=''):
        """Validate and persist a recurring schedule with its scoped execution grant."""
        spec = validate(spec)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Give the schedule a name from 1 to 80 characters.')
        if not owner_id:
            raise ValueError('A schedule needs a stable account identity.')
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('INSERT INTO recurring_schedules (id,name,owner_id,owner_name,model_name,spec,grant_token,enabled,next_at) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)',
                (identifier, name.strip(), owner_id, owner_name, model_name, json.dumps(spec), token, next_at(spec, now)))

    def rows(self, include_deleted=False):
        """List schedules, decoding each retained specification."""
        columns = ('id', 'name', 'owner_id', 'owner_name', 'model_name', 'spec', 'grant_token', 'enabled', 'next_at')
        rows = [dict(zip(columns, row)) for row in self.db.execute('SELECT '+','.join(columns)+' FROM recurring_schedules'+('' if include_deleted else ' WHERE deleted=0')+' ORDER BY name,id').fetchall()]
        for row in rows:
            row['spec'] = json.loads(row['spec'])
        return rows

    def has_active(self, identifier):
        """Return whether a schedule already has dispatching or running work."""
        return bool(self.db.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND state IN ('dispatching','running')", (identifier,)).fetchone())

    def update(self, identifier, name, spec, token, model_name='', now=None):
        """Replace a schedule specification when no incompatible occurrence is active."""
        spec = validate(spec)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Give the schedule a name from 1 to 80 characters.')
        if self.has_active(identifier):
            raise ValueError('Wait for the claimed occurrence to finish before editing this schedule.')
        with self.db:
            changed = self.db.execute('UPDATE recurring_schedules SET name=?,spec=?,grant_token=?,model_name=?,next_at=? WHERE id=? AND deleted=0',
                (name.strip(), json.dumps(spec), token, model_name, next_at(spec, time.time() if now is None else now), identifier)).rowcount
            if not changed:
                raise ValueError('No such schedule.')

    def delete(self, identifier, now=None):
        """Stop future and unsubmitted work, retaining history and submitted benchmarks."""
        now = time.time() if now is None else now
        with self.db:
            changed = self.db.execute("UPDATE recurring_schedules SET deleted=1,enabled=0,grant_token='' WHERE id=? AND deleted=0", (identifier,)).rowcount
            if not changed:
                raise ValueError('No such schedule.')
            self.db.execute("UPDATE schedule_occurrences SET state='cancelled',message='Schedule deleted before submission.',ended_at=? WHERE schedule_id=? AND state='dispatching'", (now, identifier))

    def enable(self, identifier, enabled, now=None):
        """Pause or enable a schedule and recalculate its next due occurrence."""
        row = next((row for row in self.rows() if row['id'] == identifier), None)
        if not row:
            raise ValueError('No such schedule.')
        with self.db:
            self.db.execute('UPDATE recurring_schedules SET enabled=?,next_at=? WHERE id=?',
                (int(enabled), next_at(row['spec'], time.time() if now is None else now), identifier))

    def occurrences(self, active=False, limit=25, offset=0):
        """List retained dispatch and completion records for scheduled work."""
        columns = ('id', 'schedule_id', 'due_at', 'spec', 'grant_token', 'task_token', 'state', 'message', 'ended_at')
        sql = 'SELECT '+','.join(columns)+' FROM schedule_occurrences'
        if active:
            sql += " WHERE state IN ('dispatching','running')"
        sql += ' ORDER BY due_at DESC,id DESC LIMIT ? OFFSET ?'
        rows = [dict(zip(columns, row)) for row in self.db.execute(sql, (limit, offset)).fetchall()]
        for row in rows:
            row['spec'] = json.loads(row['spec'])
            row['backup_nodes'] = [dict(zip(('node','id','state','message'), result)) for result in self.db.execute('SELECT node,backup_id,state,message FROM schedule_backup_results WHERE occurrence=? ORDER BY node', (row['id'],))]
        return rows

    def backup_results(self, occurrence, rows):
        """Keep one durable receipt per node; verified peers need no further backup."""
        with self.db:
            for row in rows:
                self.db.execute('INSERT INTO schedule_backup_results VALUES(?,?,?,?,?,?) '
                    'ON CONFLICT(occurrence,node) DO UPDATE SET backup_id=excluded.backup_id,state=excluded.state,message=excluded.message,updated=excluded.updated',
                    (occurrence, row['node'], row['id'], row['state'], row['message'], time.time()))

    def freeze_target(self, occurrence, target):
        """Persist the chosen pair before creating any native task; retries reuse it."""
        with self.db:
            original = self.db.execute('SELECT spec FROM schedule_occurrences WHERE id=?', (occurrence['id'],)).fetchone()[0]
            current = json.loads(original)
            if 'target' not in current:
                current['target'] = target
                self.db.execute('UPDATE schedule_occurrences SET spec=? WHERE id=? AND spec=?', (json.dumps(current), occurrence['id'], original))
            current = json.loads(self.db.execute('SELECT spec FROM schedule_occurrences WHERE id=?', (occurrence['id'],)).fetchone()[0])
        return current['target']

    def finish(self, identifier, state, message, now=None):
        """Persist one occurrence's terminal state and operator-visible result."""
        with self.db:
            self.db.execute('UPDATE schedule_occurrences SET state=?,message=?,ended_at=? WHERE id=?',
                (state, message, time.time() if now is None else now, identifier))

    def waiting(self, identifier, message):
        """Keep retryable dispatch open with its current readiness or wake explanation."""
        with self.db:
            self.db.execute("UPDATE schedule_occurrences SET message=? WHERE id=? AND state='dispatching'", (message[:1000], identifier))

    def claim(self, now=None):
        """Record all due slots in bounded chunks, applying lateness and overlap policies."""
        now = time.time() if now is None else now
        remaining = 50
        for row in self.rows():
            due, spec = row['next_at'], row['spec']
            while row['enabled'] and due <= now and remaining:
                following = next_at(spec, due)
                latest_missed = following <= now
                reason = ('Missed occurrence; only the latest is eligible.' if latest_missed else
                          'Missed occurrence; more than 15 minutes late.' if spec['missed'] == 'skip' and now-due > 900 else '')
                number, _ = occurrence_ids(row['id'], due)
                with self.db:
                    # The compare-and-set locks this row on either engine, even if
                    # another process has the same due snapshot.
                    changed = self.db.execute('UPDATE recurring_schedules SET next_at=? WHERE id=? AND next_at=? AND enabled=1',
                        (following, row['id'], due)).rowcount
                    if not changed:
                        break
                    overlap = self.db.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND state IN ('dispatching','running')", (row['id'],)).fetchone()
                    reason = reason or ('Skipped because the previous occurrence is still open.' if overlap else '')
                    self.db.execute('INSERT INTO schedule_occurrences VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (number, row['id'], due, json.dumps(spec), row['grant_token'], secrets.token_urlsafe(32),
                         'skipped' if reason else 'dispatching', reason or 'Waiting for the execution node before submitting scheduled work.', now if reason else None))
                due = following
                remaining -= 1


class ScheduleWorker:
    """Serialized dispatch of due schedules through existing backup and benchmark paths."""
    def __init__(self, store, compute, benchmarks, front_backup, gate, wake):
        """Bind schedule execution dependencies and initialize the dispatch lock."""
        self.store, self.compute, self.benchmarks, self.front_backup, self.gate = store, compute, benchmarks, front_backup, gate
        self.lock = asyncio.Lock()
        self.wake = wake

    async def choose_outstanding(self, occurrence, now):
        """Choose one fair missing pair using current owner-scoped inventories and evidence."""
        from benchmarks.eligibility import compatible, evidence_matches, resolve_location
        spec = occurrence['spec']
        answer = await self.compute.post('/loadcoach/schedules/'+occurrence['schedule_id']+'/inspect',
                                        headers={'x-suite-schedule':occurrence['grant_token']}, json={'due_at':occurrence['due_at']})
        answer.raise_for_status()
        inventory, candidates, unknown = answer.json(), [], []
        blocked = False
        runs = self.benchmarks.listing()
        for logical in inventory['models']:
            identities = {logical.get('model_id'), *(one.get('model_id') for one in logical.get('locations', []))}
            if spec.get('model_ids') and not identities.intersection(spec['model_ids']):
                continue
            try:
                selected, reason = resolve_location(logical, spec.get('machine',''), inventory['unavailable'])
            except ValueError as problem:
                unknown.append(str(problem))
                continue
            machine, model_id = selected['machine'], selected.get('model_id')
            environment = inventory['environments'].get(machine+':'+selected.get('engine_id','llama_cpp'), {})
            if not model_id or not selected.get('fingerprint') or not all(environment.get(key) for key in ('engine_hash','machine_hash')):
                unknown.append(logical['id']+': model identity or environment unavailable.')
                continue
            for definition in self.benchmarks.definitions():
                if not definition['enabled'] or spec.get('benchmarks') and definition['id'] not in spec['benchmarks'] or not compatible(selected, definition):
                    continue
                matching = [run for run in runs if run['model_id'] == model_id and
                            (run.get('location') or LEGACY) == machine and run['benchmark'] == definition['id']]
                if any(run['state']=='completed' and evidence_matches(run, definition, selected['fingerprint'], environment) for run in matching):
                    continue
                if any(run['state'] not in ('completed','failed','cancelled') for run in matching):
                    blocked = True
                    continue
                last = max((run['created'] for run in matching), default=0)
                if any(run['state'] in ('failed','cancelled') and now-(run.get('ended') or run['created'])<3600 for run in matching):
                    blocked = True
                    continue
                target = {'model': selected['id'], 'logical_name':logical['id'], 'model_id':model_id,
                          'scope_model_id':logical['model_id'], 'machine':machine, 'benchmark':definition['id'],
                          'fingerprint':selected['fingerprint'], 'environment':environment,
                          'preferred_node':spec.get('machine',''), 'fallback_reason':reason}
                candidates.append((last, model_id, machine, definition['id'], target))
        if candidates:
            return min(candidates, key=lambda one:one[:4])[-1], ''
        if unknown or inventory['unavailable']:
            return None, 'Deferred: node availability or model identity is unknown. ' + '; '.join(unknown[:3])
        return None, 'No eligible work: active pairs or failure cooldown.' if blocked else 'Nothing outstanding.'

    async def tick(self, now=None):
        """Dispatch due grants; front handles node wake, and benchmarks remain foreground-preemptible."""
        async with self.lock:
            if self.gate.token():
                return
            self.gate.active += 1
            try:
                for occurrence in self.store.occurrences(active=True, limit=100):
                    if occurrence['state'] == 'running':
                        run = self.benchmarks.get(occurrence['id'])
                        if run and run['state'] in ('completed', 'failed', 'cancelled'):
                            self.store.finish(occurrence['id'], run['state'], 'Benchmark '+run['state']+'. Open its run for retained evidence.', now)
                self.store.claim(now)
                for occurrence in self.store.occurrences(active=True, limit=100):
                    if self.gate.token():
                        return
                    number, spec = occurrence['id'], occurrence['spec']
                    if occurrence['state'] == 'running':
                        run = self.benchmarks.get(number)
                        if run and run['state'] in ('completed', 'failed', 'cancelled'):
                            self.store.finish(number, run['state'], 'Benchmark '+run['state']+'. Open its run for retained evidence.', now)
                        continue
                    try:
                        if spec['kind'] == 'benchmark' and not any(row['id'] == spec['benchmark'] and row['enabled'] for row in self.benchmarks.definitions()):
                            self.store.finish(number, 'failed', 'The selected benchmark is disabled or unavailable.', now)
                            continue
                        target = spec.get('target')
                        if spec['kind'] == 'outstanding':
                            if target is None:
                                target, message = await self.choose_outstanding(occurrence, time.time() if now is None else now)
                                if target is None:
                                    self.store.finish(number, 'deferred' if message.startswith('Deferred:') else 'completed', message, now)
                                    continue
                                target = self.store.freeze_target(occurrence, target)
                            definition = next((row for row in self.benchmarks.definitions() if row['id']==target['benchmark'] and row['enabled']), None)
                            if definition is None:
                                self.store.finish(number, 'failed', 'The chosen benchmark is disabled or unavailable.', now)
                                continue
                            reserved = self.benchmarks.create('', target['logical_name'], target['benchmark'], target['model_id'],
                                location=target['machine'], number=number, token=occurrence['task_token'], reserve=True) if not self.benchmarks.get(number) else number
                            if reserved is None:
                                self.store.finish(number, 'skipped', 'Matching benchmark work is already reserved.', now)
                                continue
                        answer = await self.compute.post('/loadcoach/schedules/'+occurrence['schedule_id']+'/dispatch',
                            headers={'x-suite-schedule': occurrence['grant_token']},
                            json={'due_at': occurrence['due_at'], 'task_token': occurrence['task_token'], **({'target':target} if target else {})}, timeout=120)
                        if self.gate.token():
                            return  # the persisted ID makes a lost reply recoverable
                        if answer.status_code in (429, 503):
                            self.store.waiting(number, self.response_message(answer))
                            continue
                        if not answer.is_success:
                            if target and self.benchmarks.get(number):
                                self.benchmarks.finish(number, 'failed', 'Schedule authorization or frozen target refused.')
                            detail = self.response_message(answer, 'Check owner permissions, model and backup configuration.')
                            self.store.finish(number, 'failed', f'Compute refused scheduled work (HTTP {answer.status_code}). '+detail, now)
                            if answer.status_code in (401, 403):
                                self.store.enable(occurrence['schedule_id'], False, now)
                            continue
                        remote = answer.json()
                        if spec['kind'] == 'backup':
                            outcomes = remote.get('nodes', [])
                            if remote.get('include_front', True):
                                _, front_id = occurrence_ids(occurrence['schedule_id'] + ':front', occurrence['due_at'])
                                try:
                                    await self.front_backup(front_id, spec['retention'])
                                    outcomes.append({'node':'front', 'id':front_id, 'state':'verified', 'message':'Archive verified.'})
                                except Exception as problem:
                                    outcomes.append({'node':'front', 'id':front_id, 'state':'missed', 'message':str(problem)})
                            self.store.backup_results(number, outcomes)
                            missed = [row for row in outcomes if row['state'] != 'verified']
                            message = '; '.join(row['node'] + ': ' + row['state'] + (' — ' + row['message'] if row['state'] != 'verified' else '') for row in outcomes)
                            if missed:
                                self.store.waiting(number, message + '. Only unverified nodes will be retried.')
                            elif not self.gate.token():
                                self.store.finish(number, 'completed', message, now)
                        else:
                            if spec['kind'] != 'outstanding':
                                self.benchmarks.create(remote['user'], remote['model'], spec['benchmark'], remote.get('model_id'),
                                    number=number, token=occurrence['task_token'], location=remote.get('machine',''))
                            else:
                                with self.benchmarks.db:
                                    self.benchmarks.db.execute('UPDATE benchmark_runs SET user=? WHERE id=?', (remote['user'],number))
                            if self.benchmarks.state_of(number) == 'creating':
                                self.benchmarks.activate(number, remote)
                            with self.store.db:
                                self.store.db.execute("UPDATE schedule_occurrences SET state='running',message=? WHERE id=?", ('Submitted at '+remote.get('machine',LEGACY)+'. Preferred: '+spec.get('machine','default')+'. '+(target.get('fallback_reason','') if target else remote.get('schedule_resolution',{}).get('fallback_reason','')), number))
                            self.wake.set()
                    except httpx.HTTPStatusError as problem:
                        status = problem.response.status_code
                        if status in (429, 503):
                            self.store.waiting(number, self.response_message(problem.response))
                        else:
                            detail = self.response_message(problem.response, 'Check owner permissions and node configuration.')
                            self.store.finish(number, 'failed', f'Compute refused scheduled inspection (HTTP {status}). '+detail, now)
                            if status in (401, 403):
                                self.store.enable(occurrence['schedule_id'], False, now)
                    except httpx.HTTPError:
                        self.store.waiting(number, 'The execution node is not answering. Scheduled submission will retry.')
                        continue  # a network failure may have happened after compute committed
                    except (ValueError, OSError, RuntimeError):
                        self.store.finish(number, 'failed', 'Scheduled work failed. Check Backups or the benchmark run and service output.', now)
            finally:
                self.gate.active -= 1

    @staticmethod
    def response_message(answer, fallback='Waiting for the execution node; scheduled submission will retry.'):
        """Retain a bounded operator-facing readiness or rejection explanation."""
        try:
            message = answer.json()['error']['message']
            return message[:1000] if isinstance(message, str) and message.strip() else fallback
        except (ValueError, KeyError, TypeError):
            return fallback

    async def run(self):
        """Poll due schedules while respecting the shared maintenance barrier."""
        while True:
            await self.gate.wait()
            try:
                await self.tick()
            except Exception:
                # Monitoring/maintenance must keep functioning if this store fails.
                print('weightroom: schedule processing failed; retrying.', flush=True)
            await asyncio.sleep(15)
