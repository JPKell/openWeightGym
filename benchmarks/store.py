"""The benchmark package's own evidence and definitions, identified by LoadCoach task IDs."""

import hashlib
import json
import os
import secrets
import time

from db import DatabaseSource, database, local_path, migrate
from db.schemas import BENCHMARKS as BENCHMARK_MIGRATIONS
from db.text_codec import pack_text, unpack_text

from . import BENCHMARKS, MODULES, TEXT, quality
from .scoring import MODEL_VALUES, calculate, formula_tree
from . import custom

TERMINAL = {"completed", "failed", "cancelled"}
RUN_COLUMNS = 'model,benchmark,fingerprint,subject,environment,state,created,started,ended,reason,messages,user,location,model_id'

class Benchmarks:
    """Local benchmark records; the HTTP worker reconciles their separate LoadCoach batches."""

    def __init__(self, path: DatabaseSource):
        """Open benchmark storage and apply its portable schema migrations."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        if file := local_path(path):
            os.chmod(file, 0o600)  # task-scoped credentials live here, never the user's session key
        migrate(source, self.db, "weightroom.benchmarks", BENCHMARK_MIGRATIONS)
        self._datasets = {}
        with self.db:
            self.db.execute('INSERT INTO benchmark_state(id,paused) VALUES(1,0) ON CONFLICT(id) DO NOTHING')
            self.db.executemany('INSERT INTO benchmark_definitions(id,enabled,formula) VALUES(?,1,?) ON CONFLICT(id) DO NOTHING', [(b['id'], b['formula']) for b in BENCHMARKS])
            self.db.execute("UPDATE benchmark_runs SET state='queued', reason='Service restarted; incomplete sample will be retried.' WHERE state='running'")
        self.regrade()

    def module(self, kind: str, protocol: dict | None = None):
        """Resolve an imported version within this store; never mutate the global registry."""
        if kind in MODULES:
            return MODULES[kind]
        version = (protocol or {}).get('version') or self.db.execute('SELECT MAX(version) FROM benchmark_datasets WHERE suite=?', (kind,)).fetchone()[0]
        if (kind, version) in self._datasets:
            return self._datasets[(kind, version)]
        row = self.db.execute('SELECT dataset,sha256 FROM benchmark_datasets WHERE suite=? AND version=?', (kind,version)).fetchone()
        if row is None:
            raise ValueError('Unknown benchmark or dataset version.')
        if len(self._datasets) >= 32:
            self._datasets.pop(next(iter(self._datasets)))
        dataset = json.loads(row[0])
        if custom.digest(dataset) != row[1]:
            raise ValueError('Stored dataset checksum changed. Restore the immutable version from a verified recovery set.')
        self._datasets[(kind,version)] = module = custom.Dataset(dataset, row[1])
        return module

    def import_dataset(self, dataset: dict, by: str) -> dict:
        """Validate and persist an immutable benchmark dataset version."""
        dataset = custom.validate(dataset)
        kind, version, sha256 = 'custom_' + dataset['id'], dataset['version'], custom.digest(dataset)
        with self.db:
            existing = self.db.execute('SELECT sha256 FROM benchmark_datasets WHERE suite=? AND version=?', (kind, version)).fetchone()
            if existing:
                if existing[0] != sha256:
                    raise ValueError('This dataset version already exists with different content. Increase version.')
                return {'benchmark': kind, 'version': version, 'sha256': sha256, 'created': False}
            latest = self.db.execute('SELECT MAX(version) FROM benchmark_datasets WHERE suite=?', (kind,)).fetchone()[0]
            if latest is not None and version <= latest:
                raise ValueError('New dataset versions must increase monotonically.')
            if latest is None and self.db.execute('SELECT COUNT(DISTINCT suite) FROM benchmark_datasets').fetchone()[0] >= 64:
                raise ValueError('At most 64 imported benchmark names are supported.')
            self.db.execute('INSERT INTO benchmark_datasets VALUES(?,?,?,?,?,?,?) ON CONFLICT(suite,version) DO NOTHING', (kind,version,custom.encoded(dataset),sha256,time.time(),by,dataset['name']))
            if self.db.execute('SELECT sha256 FROM benchmark_datasets WHERE suite=? AND version=?', (kind,version)).fetchone()[0] != sha256:
                raise ValueError('That version was imported concurrently with different content. Increase version.')
            self.db.execute("INSERT INTO benchmark_definitions(id,enabled,formula,green) VALUES(?,1,'accuracy','high') ON CONFLICT(id) DO NOTHING", (kind,))
        return {'benchmark': kind, 'version': version, 'sha256': sha256, 'created': True}

    def delete_dataset(self, kind: str) -> int:
        """Remove every version of an imported case set that no run uses. Returns how many versions went.

        A run keeps only the dataset's name and version, and is scored again from it, so a set
        that any run used stays.
        """
        with self.db:
            if not kind.startswith('custom_') or not self.db.execute('SELECT 1 FROM benchmark_datasets WHERE suite=?', (kind,)).fetchone():
                raise ValueError('Choose an imported dataset.')
            used = self.db.execute('SELECT COUNT(*) FROM benchmark_runs WHERE benchmark=?', (kind,)).fetchone()[0]
            if used:
                raise ValueError(f'{used} benchmark run{"" if used == 1 else "s"} used this case set, so it is kept for their results.')
            gone = self.db.execute('DELETE FROM benchmark_datasets WHERE suite=?', (kind,)).rowcount
            self.db.execute('DELETE FROM benchmark_definitions WHERE id=?', (kind,))
        self._datasets = {key: module for key, module in self._datasets.items() if key[0] != kind}
        return gone

    def regrade(self) -> int:
        """Score saved answers again where the grader has moved on since they were scored. Returns how many runs it did.

        A run keeps each raw answer and the case it answered, so nothing is asked of a model
        again. What the model wrote is not touched: only what was made of it, the final answer,
        its checks and its counts. The run then says which grader it was scored by, so this is
        done once. How the model was asked stays as it was, and that is what `version` tells.
        """
        done = 0
        for number, kind, subject in self.db.execute("SELECT id,benchmark,subject FROM benchmark_runs WHERE state IN ('completed','failed','cancelled')").fetchall():
            subject = json.loads(subject)
            module = self.module(kind, subject.get('protocol'))
            grader = module.PROTOCOL.get('grader_version')
            if grader is None or subject.get('protocol', {}).get('grader_version') == grader:
                continue
            with self.db:
                for sample, values, output, evidence in self.db.execute('SELECT s.sample,s.measurements,s.output,e.evidence FROM benchmark_samples s JOIN benchmark_evidence e '
                                                                       'ON e.run=s.run AND e.sample=s.sample WHERE s.run=?', (number,)).fetchall():
                    values, evidence = json.loads(values), json.loads(evidence)
                    output = self._sample_text(number, sample, 'output', output)
                    extract = getattr(module, 'saved_answer', None)
                    answer = extract(output, evidence.get('prompt', '')) if extract else quality.evaluation_answer(output, evidence)
                    if values.get('unsupported'):
                        continue
                    metrics, checks = module.grade(evidence['case'], answer if values.get('success') else '')
                    self.db.execute('UPDATE benchmark_samples SET measurements=? WHERE run=? AND sample=?', (json.dumps({**values, **metrics}, allow_nan=False), number, sample))
                    self.db.execute('UPDATE benchmark_evidence SET evidence=? WHERE run=? AND sample=?', (json.dumps({**evidence, 'answer': answer, 'checks': checks}, allow_nan=False), number, sample))
                subject.setdefault('protocol', {})['grader_version'] = grader
                self.db.execute('UPDATE benchmark_runs SET subject=? WHERE id=?', (json.dumps(subject), number))
            self.message(number, f'Saved answers scored again by grader version {grader}. No model was asked again.')
            done += 1
        return done

    def definitions(self) -> list[dict]:
        """List built-in and custom benchmark definitions with saved overrides."""
        saved = {key: (bool(on), formula, green, server) for key, on, formula, green, server in self.db.execute('SELECT id,enabled,formula,green,server FROM benchmark_definitions')}
        imported = [custom.definition({'id':kind.removeprefix('custom_'), 'name':name, 'version':version}, sha256)
                    for kind, version, name, sha256 in self.db.execute('SELECT b.suite,b.version,b.name,b.sha256 FROM benchmark_datasets b '
                        'WHERE b.version=(SELECT MAX(d.version) FROM benchmark_datasets d WHERE d.suite=b.suite) ORDER BY b.suite')]
        # A quality benchmark keeps one model server for its cases unless told otherwise; a measurement loads one for each sample.
        return [dict(bench, enabled=saved[bench['id']][0], formula=saved[bench['id']][1], green=saved[bench['id']][2],
                     server=saved[bench['id']][3] or ('reuse' if bench.get('quality') else 'fresh')) for bench in [*BENCHMARKS, *imported]]

    def configure(self, key: str, enabled: bool, formula: str, green: str | None = None, server: str | None = None) -> None:
        """Save selection, score formula, which end of the heatmap is green and whether cases share a model server."""
        bench = next((b for b in self.definitions() if b['id'] == key), None)
        if bench is None:
            raise ValueError('Unknown benchmark.')
        if green is not None and green not in ('high', 'low'):
            raise ValueError('Choose high or low values for green.')
        if server is not None and (server not in ('reuse', 'fresh') or server == 'reuse' and not bench.get('quality')):
            raise ValueError('Only a quality benchmark may reuse one model server; choose reuse or fresh.')
        formula_tree(formula, {kpi[0] for kpi in bench['kpis']} | MODEL_VALUES)
        with self.db:
            self.db.execute('UPDATE benchmark_definitions SET enabled=?, formula=?, green=coalesce(?,green), server=coalesce(?,server) WHERE id=?', (enabled, formula, green, server, key))

    def create(self, user: str, model: str, kind: str, model_id: str | None = None, *, location: str = "", number=None, token=None, reserve=False) -> str:
        """Persist a task intent before contacting LoadCoach, so a lost reply is recoverable."""
        number, token = number or 'batch_' + secrets.token_hex(12), token or secrets.token_urlsafe(32)
        with self.db:
            found = self.db.execute('SELECT user,model_id,benchmark,token,location FROM benchmark_runs WHERE id=?', (number,)).fetchone()
            if found:
                if found != (user, model_id, kind, token, location):
                    raise ValueError('That benchmark occurrence ID is already in use.')
                return number
            if reserve:
                from benchmarks.eligibility import reservation_key
                key = reservation_key(model_id or model, location, kind)
                if self.db.execute("SELECT id FROM benchmark_runs WHERE model_id=? AND COALESCE(NULLIF(location,''),'compute')=? AND benchmark=? AND state IN ('creating','queued','running')", (model_id,location or 'compute',kind)).fetchone():
                    return None
                self.db.execute("DELETE FROM benchmark_reservations WHERE key=? AND run IN (SELECT id FROM benchmark_runs WHERE state IN ('completed','failed','cancelled'))", (key,))
                if not self.db.execute('INSERT INTO benchmark_reservations(key,run) VALUES(?,?) ON CONFLICT(key) DO NOTHING', (key, number)).rowcount:
                    return None
            subject = {'protocol': self.module(kind).PROTOCOL} if kind not in MODULES else {}
            self.db.execute('INSERT INTO benchmark_runs(id,model,model_id,benchmark,fingerprint,subject,environment,state,created,user,token,location) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (number, model, model_id, kind, '', json.dumps(subject), '{}', 'creating', time.time(), user, token, location))
        return number

    def activate(self, number: str, task: dict) -> None:
        """Freeze the accepted LoadCoach subject/environment and make the durable intent runnable."""
        subject = task['subject']
        previous = self.execution(number)
        kind = previous['benchmark']
        protocol = previous['subject']['protocol'] if kind not in MODULES else MODULES[kind].PROTOCOL
        subject['server'] = next((b['server'] for b in self.definitions() if b['id'] == kind), 'fresh')  # frozen with the run
        subject['protocol'] = {'parallel':1, 'temperature':0, 'seed':42, 'ignore_eos':True, 'cache_prompt':False,
                               'text_sha256':hashlib.sha256(TEXT.encode()).hexdigest(), 'load_poll_seconds':.02, **protocol}
        with self.db:
            self.db.execute("UPDATE benchmark_runs SET fingerprint=?,subject=?,environment=?,model_id=coalesce(model_id,?),location=CASE WHEN location='' THEN ? ELSE location END,state='queued' WHERE id=?",
                            (task['fingerprint'], json.dumps(subject), json.dumps(task['environment']), task.get('model_id') or subject.get('model_id'), task.get('machine', ''), number))
        self.message(number, 'Queued for empty GPUs.')

    def execution(self, number: str) -> dict:
        """Read only execution identity, without loading earlier long prompts or outputs."""
        model,kind,mark,subject,environment,location=self.db.execute('SELECT model,benchmark,fingerprint,subject,environment,location FROM benchmark_runs WHERE id=?',(number,)).fetchone()
        return {'id':number,'model':model,'benchmark':kind,'fingerprint':mark,'subject':json.loads(subject),'environment':json.loads(environment),'location':location}

    def probe_failure(self, number: str, sample: str, reason: str) -> int:
        """Count failed native attempts durably; pausing or restarting cannot reset the limit."""
        with self.db:
            self.db.execute('INSERT INTO benchmark_probe_failures VALUES(?,?,1,?) '
                            'ON CONFLICT(run,sample) DO UPDATE SET attempts=attempts+1, reason=excluded.reason',
                            (number,sample,reason))
        return self.probe_attempts(number, sample)[0]

    def probe_attempts(self, number: str, sample: str) -> tuple[int, str]:
        """Read the persistent native-failure limit and its last explanation."""
        return self.db.execute('SELECT attempts,reason FROM benchmark_probe_failures WHERE run=? AND sample=?',
                               (number,sample)).fetchone() or (0, '')

    def has_sample(self, number: str, sample: str) -> bool:
        """Return whether this run already retains the specified sample."""
        return self.db.execute('SELECT 1 FROM benchmark_samples WHERE run=? AND sample=?',(number,sample)).fetchone() is not None

    def token(self, number: str) -> str:
        """Read the scoped execution token for the selected durable run."""
        return self.db.execute('SELECT token FROM benchmark_runs WHERE id=?', (number,)).fetchone()[0]

    def running_job(self, number: str) -> dict | None:
        """Lightweight active-job metadata, without samples or task credentials."""
        if self.paused():
            return None
        row = self.db.execute("SELECT id,model,benchmark,user,started FROM benchmark_runs WHERE id=? AND state='running'", (number,)).fetchone()
        return dict(zip(('id', 'model', 'benchmark', 'user', 'started'), row)) if row else None

    def get(self, number: str, detail: bool = False) -> dict | None:
        """Return one retained run with its configuration and current execution state."""
        row = self.db.execute(f'SELECT {RUN_COLUMNS} FROM benchmark_runs WHERE id=?', (number,)).fetchone()
        if row is None:
            return None
        definitions = {one['id']: one for one in self.definitions()}
        return self._run(number, row, detail, definitions)

    def _run(self, number, row, detail, definitions, samples=None) -> dict:
        """Summarize one run from a bounded report's already-read rows and definitions."""
        model, kind, mark, subject, environment, state, created, started, ended, reason, messages, user, location, model_id = row
        samples = self.samples(number, detail) if samples is None else samples
        subject = json.loads(subject)
        module = self.module(kind, subject.get('protocol'))
        metrics = module.summarize(samples)
        gpu = [sample['values']['peak_gpu_mib'] for sample in samples if sample['values'].get('success')
               and type(sample['values'].get('peak_gpu_mib')) in (int, float)]
        if gpu:
            metrics['gpu_vram_mib'] = max(gpu)
        definition = definitions[kind]
        result = {'id': number, 'model': model, 'benchmark': kind, 'fingerprint': mark, 'environment': json.loads(environment), 'location': location, 'state': state,
                  'created': created, 'started': started, 'ended': ended, 'reason': reason, 'metrics': metrics,
                  'score': calculate(definition['formula'], {**subject.get('values', {}), **metrics}) if state == 'completed' else None,
                  'formula': definition['formula'], 'samples_count': len(samples), 'user': user,
                  # The version of the benchmark this run was made under, and the one the benchmark is at now.
                  'version': subject.get('protocol', {}).get('version', 1), 'latest_version': definition['version']}
        current_protocol = module.PROTOCOL
        result['current_protocol'] = all(subject.get('protocol', {}).get(key) == value for key, value in current_protocol.items())
        result['current_server'] = subject.get('server', 'fresh') == definition.get('server', 'fresh')
        result['context_size'] = subject.get('settings', {}).get('flags', {}).get('ctx-size')
        from .identity import variant
        result.update(variant(model, subject))
        result['model_id'] = model_id
        if detail:
            result.update(subject=subject, samples=samples, messages=json.loads(messages))
            result['plan'] = self.plan(number)
            from .progress import progress
            result['progress'] = progress(result, time.time())
            if result['plan']:
                from .quality import breakdown
                result['breakdown'] = getattr(module, 'breakdown', breakdown)(samples)
        return result

    def listing(self) -> list[dict]:
        """Read run summaries in batches, excluding retained output, logs and evidence."""
        definitions = {one['id']: one for one in self.definitions()}
        samples = {}
        for number, sample, at, measurements in self.db.execute(
            'SELECT run,sample,at,measurements FROM benchmark_samples ORDER BY run,at'
        ):
            samples.setdefault(number, []).append({'id': sample, 'at': at, 'values': json.loads(measurements)})
        return [self._run(row[0], row[1:], False, definitions, samples.get(row[0], []))
                for row in self.db.execute(f'SELECT id,{RUN_COLUMNS} FROM benchmark_runs ORDER BY created DESC')]

    def role_snapshot(self, model_ids=None, number=None, *, runs=None) -> dict:
        """Publish compact completed evidence, never prompts, outputs or task credentials."""
        definitions = [{"id": one["id"], "name": one["name"], "formula": one["formula"],
                        "direction": one['green'], "version": one['version']} for one in self.definitions()]
        rows = [self.get(number)] if number else runs if runs is not None else self.listing()
        latest = {}
        for run in sorted((one for one in rows if one), key=lambda one: one['ended'] or one['created'], reverse=True):
            if not run or run['state'] != 'completed' or run['score'] is None or not run.get('model_id') or not run.get('fingerprint') or not run.get('formula') or (model_ids is not None and run['model_id'] not in model_ids):
                continue
            key = (run['model_id'], run['fingerprint'], run['benchmark'], run['version'])
            if key in latest:
                continue
            metrics = run['metrics']
            latest[key] = {"model_id": run['model_id'], "fingerprint": run['fingerprint'], "benchmark": run['benchmark'],
                "version": run['version'], "run": run['id'], "measured_at": run['ended'] or run['created'], "score": run['score'],
                "formula": run['formula'],
                "engine_hash": run['environment'].get('engine_hash'), "machine_hash": run['environment'].get('machine_hash'),
                "speed": metrics.get('generation_tokens_per_second'), "load": metrics.get('load_seconds')}
        return {"definitions": definitions, "results": list(latest.values())}

    def bind_models(self, catalogue: list[dict]) -> None:
        """Migrate legacy name references once using authorized, timestamped model identities.

        Bindings never change after assignment. A deleted/recreated label cannot capture a
        prior ID; missing or ambiguous history stays unbound instead of being guessed.
        """
        with self.db:
            rows = self.db.execute('SELECT id,model,created,subject FROM benchmark_runs WHERE model_id IS NULL').fetchall()
            for number, name, created, subject in rows:
                recorded = json.loads(subject).get('model_id')
                choices = {model['model_id'] for model in catalogue if model.get('model_id') and any(
                    period['name']==name and period['since']<=created and (period['until'] is None or created<period['until'])
                    for period in model.get('name_history', []))}
                identity = recorded or (next(iter(choices)) if len(choices)==1 else None)
                if identity:
                    self.db.execute('UPDATE benchmark_runs SET model_id=? WHERE id=? AND model_id IS NULL',(identity,number))

    def _sample_text(self, number, sample, field, value):
        """Decode retained model output while preserving legacy text compatibility."""
        row = self.db.execute('SELECT codec FROM benchmark_sample_encodings WHERE run=? AND sample=? AND field=?',
                              (number, sample, field)).fetchone()
        return unpack_text(value, row[0] if row else '')

    def samples(self, number: str, detail: bool = True) -> list[dict]:
        """Read small measurement rows for scores; fetch output/log text only for run details."""
        fields = 'sample,at,measurements,output,log' if detail else 'sample,at,measurements'
        result = []
        for row in self.db.execute(f'SELECT {fields} FROM benchmark_samples WHERE run=? ORDER BY at', (number,)):
            point = {'id': row[0], 'at': row[1], 'values': json.loads(row[2])}
            if detail:
                point.update(output=self._sample_text(number, row[0], 'output', row[3]),
                             log=self._sample_text(number, row[0], 'log', row[4]))
                evidence = self.db.execute('SELECT evidence FROM benchmark_evidence WHERE run=? AND sample=?', (number,row[0])).fetchone()
                if evidence:
                    point['evidence'] = json.loads(evidence[0])
            result.append(point)
        return result

    def state_of(self, number: str) -> str:
        """A cheap supervision check which does not read or rescore measurements."""
        return self.db.execute('SELECT state FROM benchmark_runs WHERE id=?', (number,)).fetchone()[0]

    def pending(self) -> bool:
        """Whether supervision is needed; an empty or fully settled queue sleeps until woken."""
        return self.db.execute("SELECT 1 FROM benchmark_runs WHERE state NOT IN ('completed','failed','cancelled') OR synced=0 LIMIT 1").fetchone() is not None

    def next_run(self) -> str | None:
        """Return the next durable run eligible for worker execution."""
        row = self.db.execute("SELECT id FROM benchmark_runs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        return row[0] if row else None

    def keep(self, number: str, sample: str, values: dict, output: str, log: str, evidence: dict | None = None) -> None:
        """Only completed samples are saved; a restart or preemption retries anything absent."""
        packed = {field: pack_text(value) for field, value in (
            ('output', output if evidence is not None else output[:32000]), ('log', log[-16000:]))}
        with self.db:
            inserted = self.db.execute('INSERT INTO benchmark_samples VALUES(?,?,?,?,?,?) ON CONFLICT(run,sample) DO NOTHING', (number, sample, time.time(), json.dumps(values, allow_nan=False), packed['output'][0], packed['log'][0])).rowcount
            if inserted:
                for field, (_, codec) in packed.items():
                    if codec:
                        self.db.execute('INSERT INTO benchmark_sample_encodings VALUES(?,?,?,?)', (number, sample, field, codec))
            if evidence is not None:
                self.db.execute('INSERT INTO benchmark_evidence VALUES(?,?,?) ON CONFLICT(run,sample) DO NOTHING', (number,sample,json.dumps(evidence,allow_nan=False)))

    def plan(self, number: str, value: dict | None = None) -> dict | None:
        """Freeze case inputs and answer keys before the first probe; reuse them on restart."""
        if value is not None:
            with self.db:
                self.db.execute('INSERT INTO benchmark_plans VALUES(?,?) ON CONFLICT(run) DO NOTHING', (number,json.dumps(value,allow_nan=False)))
        row = self.db.execute('SELECT plan FROM benchmark_plans WHERE run=?', (number,)).fetchone()
        return json.loads(row[0]) if row else None

    def message(self, number: str, text: str) -> None:
        """Persist the current operator-visible progress message."""
        row = self.db.execute('SELECT messages FROM benchmark_runs WHERE id=?', (number,)).fetchone()
        messages = json.loads(row[0])
        messages.append({'at': time.time(), 'text': text})
        with self.db:
            self.db.execute('UPDATE benchmark_runs SET messages=? WHERE id=?', (json.dumps(messages[-500:]), number))

    def state(self, number: str, state: str, reason: str = '') -> None:
        """Persist the selected run's execution state and progress message."""
        with self.db:
            self.db.execute("UPDATE benchmark_runs SET state=?, reason=?, started=CASE WHEN ?='running' THEN coalesce(started,?) ELSE started END WHERE id=?", (state, reason, state, time.time(), number))
        if reason:
            self.message(number, reason)

    def finish(self, number: str, state: str, reason: str = '') -> None:
        """Retain every sample and mark the owning batch for closure by the HTTP reconciler."""
        self.state(number, state, reason)
        with self.db:
            self.db.execute('UPDATE benchmark_runs SET ended=? WHERE id=?', (time.time(), number))
            self.db.execute('UPDATE benchmark_runs SET synced=0 WHERE id=?', (number,))

    def unsynced(self) -> list[dict]:
        """Terminal runs needing their batch closed; retry after either service restarts."""
        return [self.get(row[0]) for row in self.db.execute("SELECT id FROM benchmark_runs WHERE state IN ('completed','failed','cancelled') AND synced=0")]

    def synced(self, number: str) -> None:
        """Mark the run's role evidence as successfully published."""
        with self.db:
            self.db.execute('UPDATE benchmark_runs SET synced=1 WHERE id=?', (number,))

    def paused(self, value: bool | None = None) -> bool:
        """Read or persist the benchmark worker's global pause state."""
        if value is not None:
            with self.db:
                self.db.execute('UPDATE benchmark_state SET paused=? WHERE id=1', (value,))
        return bool(self.db.execute('SELECT paused FROM benchmark_state WHERE id=1').fetchone()[0])
