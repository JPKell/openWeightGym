"""Benchmark plans executed through LoadCoach's HTTP task/probe API.

This process never opens LoadCoach's database, starts a model process or reserves GPUs itself.
Its own database retains samples and queue controls; scoped task tokens survive session expiry.
"""

import asyncio
import hashlib
import json
import time

import httpx

from . import TEXT, quality
from .store import TERMINAL

RETRY_SECONDS = 1  # a failed native attempt waits this times 2, then times 4, before its retry


class Retry(Exception):
    """An incomplete sample can be retried after preemption or an unavailable service."""


class ProbeFailure(Retry):
    """A native model failed; unlike waiting for compute, retries must be bounded."""


class BenchmarkWorker:
    """Durable low-priority benchmark execution using existing openWeightGym task APIs."""
    def __init__(self, store, client):
        """Bind durable benchmark storage, authenticated transport and wake notification."""
        self.store, self.client = store, client
        self.creating = set()
        self.current = None
        self.task = None
        self.wake = asyncio.Event()

    async def request(self, number, method='GET', suffix='', **kwargs):
        """Use only the run's scoped task token; never retain a user's API/session key."""
        try:
            response = await self.client.request(method, '/loadcoach/tasks/' + number + suffix,
                headers={'x-loadcoach-task': self.store.token(number), **kwargs.pop('headers', {})}, **kwargs)
        except httpx.HTTPError as problem:
            raise Retry('LoadCoach is unavailable; completed samples are kept.') from problem
        if suffix == '/probe' and response.status_code == 502:
            raise ProbeFailure(response.json()['error']['message'])
        if response.status_code in (409,429) or response.status_code >= 500:
            raise Retry(response.json()['error']['message'])
        if response.status_code == 400:
            raise ValueError(response.json()['error']['message'])
        response.raise_for_status()
        return response.json()

    async def reconcile(self):
        """Recover interrupted creation, observe batch cancellation, and close terminal batches."""
        for run in self.store.listing():
            number = run['id']
            if number in self.creating or (run['state']=='creating' and time.time()-run['created'] < 30):
                continue
            if run['state'] in TERMINAL:
                continue
            try:
                remote = await self.request(number)
                if run['state'] == 'creating':
                    self.store.activate(number, remote)
                if remote['status'] == 'cancelled':
                    self.store.finish(number, 'cancelled', 'Stopped through Batches.')
                elif remote['status'] == 'completed':
                    self.store.finish(number, 'failed', 'The owning task ended before these measurements completed.')
                elif not remote['allowed']:
                    self.store.finish(number, 'failed', 'The submitting account no longer has permission for this benchmark/model.')
            except Retry:
                return
            except httpx.HTTPStatusError as problem:
                if problem.response.status_code == 404:
                    self.store.finish(number, 'failed', 'Task creation was interrupted before LoadCoach accepted it.')
        for run in self.store.unsynced():
            try:
                snapshot = self.store.role_snapshot(number=run['id']) if run['state'] == 'completed' else None
                await self.request(run['id'], 'POST', '/finish', json={'state':run['state'],
                    'result': {'benchmark': run['benchmark'], 'metrics':run['metrics'], 'error':run['reason'] if run['state']=='failed' else None},
                    'role_evidence': snapshot if snapshot and snapshot['results'] else None})
                self.store.synced(run['id'])
            except Retry:
                return
            except httpx.HTTPStatusError as problem:
                if problem.response.status_code == 404:
                    self.store.synced(run['id'])  # creation never reached LoadCoach

    async def run(self):
        """Supervise one run while syncing durable batch controls, including paused jobs."""
        try:
            while True:
                if gate := getattr(self, "maintenance", None):
                    await gate.wait()
                self.wake.clear()
                await self.reconcile()
                if self.task and not self.task.done() and (self.store.paused() or self.store.state_of(self.current) != 'running'):
                    if not self.task.cancelling():
                        self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
                if self.task and self.task.done():
                    try:
                        await self.task
                    except (Retry, asyncio.CancelledError) as problem:
                        if self.store.state_of(self.current) == 'running':
                            self.store.state(self.current, 'queued', 'Yielded; incomplete sample discarded. Completed samples are kept. ' + str(problem))
                    except Exception as problem:
                        if self.store.state_of(self.current) not in TERMINAL:
                            self.store.finish(self.current, 'failed', str(problem))
                    self.current, self.task = None, None
                if not self.task and not self.store.paused() and (number := self.store.next_run()):
                    self.current = number
                    self.store.state(number, 'running')
                    self.task = asyncio.create_task(self.measure(number))
                try:
                    # No polling or HTTP traffic when there is no work left to supervise.
                    pending = self.store.pending()
                    await asyncio.wait_for(self.wake.wait(), .5 if pending else None)
                except TimeoutError:
                    pass
        finally:
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def check(self, number):
        """Reject changed model/engine identities before asking for another probe."""
        run = self.store.execution(number)
        remote = await self.request(number)
        if remote['status'] == 'cancelled':
            self.store.finish(number, 'cancelled', 'Stopped through Batches.')
            raise asyncio.CancelledError
        if not remote['allowed']:
            raise ValueError('The submitting account no longer has permission for this benchmark/model.')
        if remote['current_fingerprint'] != run['fingerprint']:
            raise ValueError('Model fingerprint changed during the run; submit a new run.')
        if any(remote['current_environment'].get(key) != run['environment'].get(key) for key in ('engine_hash','machine_hash')):
            raise ValueError('Engine or machine changed during this run; submit a new run.')
        return run

    async def measure(self, number):
        """Dispatch one run to its benchmark protocol and persist completed samples."""
        run = await self.check(number)
        settings = run['subject']['settings']
        module = self.store.module(run['benchmark'])
        multimedia = settings.get('engine') in ('sdcpp', 'audiocpp')
        classifier = bool(run['subject'].get('metadata', {}).get('decision_type'))
        if run['benchmark'] != 'load' and bool(module.DEFINITION.get('classifier')) != classifier:
            raise ValueError('Classifier benchmarks require a Classifier model; generation benchmarks require a text model.')
        if multimedia and run['benchmark'] != 'load':
            raise ValueError('Image and audio models support load-time benchmarks only; context is unavailable.')
        if run['benchmark'] != 'load' and bool(module.DEFINITION.get('embedding')) != bool(settings['flags'].get('embedding')):
            raise ValueError('Choose embedder benchmarks for embedding models and generation benchmarks for other models.')
        ceiling = int(run['subject']['values'].get('trained_context') or settings['flags']['ctx-size'])
        if ceiling < 128 and not multimedia:
            raise ValueError('The model must declare at least 128 context tokens.')
        await module.measure(self, run, ceiling)
        if self.store.state_of(number) == 'running':
            self.store.finish(number, 'completed', 'All measurements completed.')

    async def isolated(self, number, sample, body):
        """Try a native probe, with two delayed retries before failing that run visibly."""
        if gate := getattr(self, "maintenance", None):
            await gate.wait()
        attempts, reason = self.store.probe_attempts(number, sample)
        if attempts >= 3:
            raise ValueError(f'{sample}: already reached the limit of 3 failed native attempts. {reason}')
        if 'evaluation' in body or 'dialogue' in body or 'decisions' in body:  # a case, not a measurement: it may share its run's model server
            body = {**body, 'reuse': self.store.execution(number)['subject'].get('server') == 'reuse'}
        try:
            return await self.request(number, 'POST', '/probe', json=body,
                headers={'x-loadcoach-probe-id': hashlib.sha256(sample.encode()).hexdigest()})
        except ProbeFailure as problem:
            attempts = self.store.probe_failure(number, sample, str(problem))
            if attempts >= 3:
                raise ValueError(f'{sample}: stopped after {attempts} failed native attempts. '
                                 f'Completed samples are kept. {problem}') from problem
            delay = RETRY_SECONDS * 2 ** attempts
            self.store.message(number, f'{sample}: native attempt {attempts}/3 failed; retry in {delay}s. {problem}')
            await asyncio.sleep(delay)
            raise Retry(f'{sample}: retrying after native failure {attempts}/3.') from problem

    async def probe(self, number, sample, context, inputs, outputs, repeat=0, *, warmup_outputs=0, warmup_embeddings=False, capacity_is_result=False):
        """Ask LoadCoach for a single isolated probe, then retain its completed evidence locally."""
        if self.store.has_sample(number, sample):
            return
        run = await self.check(number)
        embedding = bool(run['subject']['settings']['flags'].get('embedding'))
        self.store.message(number, f'{sample}: requesting an isolated probe with {context:,} context tokens.' if context else
                           f'{sample}: measuring native model loading; token context is unavailable.')
        try:
            result = await self.isolated(number, sample, {'context':context, 'inputs':inputs, 'outputs':outputs,
                'warmup_outputs':warmup_outputs, 'text':TEXT,
                **({'embedding':True, 'warmup_embeddings':warmup_embeddings} if embedding else {})})
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.request(number, 'POST', '/interrupt', timeout=5))
            except (Retry, httpx.HTTPError):
                pass
            raise
        await self.check(number)
        values = dict(result['values'], repetition=repeat + 1)
        if not values['success'] and not capacity_is_result:
            raise ValueError(values.get('error', 'The measurement could not finish.'))
        self.store.keep(number, sample, values, result['output'], result['log'],
                        result.get('evidence') if embedding or values.get('device') == 'cpu' else None)
        self.store.message(number, f'{sample}: ' + ('completed.' if values['success'] else 'does not fit.'))

    async def classify(self, number, sample, body, grade=None, case=None):
        """Keep typed decision evidence through the normal isolated native probe path."""
        if self.store.has_sample(number, sample):
            return
        run = await self.check(number)
        self.store.message(number, f'{sample}: evaluating typed decisions.')
        result = await self.isolated(number, sample, {'context': run['subject']['settings']['flags']['ctx-size'],
            'inputs': 0, 'outputs': 0, 'decisions': body})
        await self.check(number)
        values, evidence = result['values'], result.get('evidence', {})
        if not values['success']:
            raise ValueError(values.get('error', 'Classifier probe failed.'))
        if grade:
            metrics, checks = grade(case, json.loads(result['output']))
            values = {**values, **metrics}
            evidence = {**evidence, 'case': case, 'checks': checks}
        self.store.keep(number, sample, values, result['output'], result['log'], evidence)

    async def evaluate(self, number, case, grade):
        """Send only the question/evidence, score the answer locally and atomically save the case."""
        sample = case['id']
        if self.store.has_sample(number, sample):
            return
        run = await self.check(number)
        if case.get('requires_tools'):
            flags = run['subject']['settings']['flags']
            if (not run['subject'].get('metadata',{}).get('takes_tools')
                    or flags.get('jinja') is False or flags.get('no-jinja') is True):
                self.store.keep(number,sample,{'success':False,'unsupported':True,'category':case.get('category',''),
                    'error':'The recorded template does not advertise tools, or jinja is explicitly disabled.'},
                    '','',{'case':case,'checks':[]})
                return
        self.store.message(number, f'{sample}: evaluating at {case["context"]:,} context tokens.')
        try:
            result = await self.isolated(number, sample, {'context':case['context'],
                'inputs':case.get('inputs',0), 'outputs':case['outputs'], 'evaluation':case['request']})
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.request(number, 'POST', '/interrupt', timeout=5))
            except (Retry, httpx.HTTPError):
                pass
            raise
        await self.check(number)
        # A model may reason before it answers. The raw output is kept whole; only the final answer is graded.
        answer = quality.evaluation_answer(result['output'], result.get('evidence',{}))
        metrics, checks = await asyncio.to_thread(grade, case, answer if result['values']['success'] else '')
        values = {**result['values'], **metrics, 'category':case.get('category',''), 'target_input_tokens':case.get('inputs',0)}
        evidence = {**result.get('evidence',{}), 'case':case, 'answer':answer, 'checks':checks}
        self.store.keep(number, sample, values, result['output'], result['log'], evidence)
        self.store.message(number, f'{sample}: ' + ('passed.' if metrics['passed'] else 'did not pass.') +
                           (' Probe unavailable: '+values.get('error','') if not values['success'] else ''))
