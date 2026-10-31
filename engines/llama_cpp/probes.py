"""llama.cpp isolated model probes; no benchmark plans or result storage.

LoadCoach alone owns processes, reservations and permission checks. Callers choose a context
and token workload; one probe yields to ordinary requests and reports native timing evidence.
"""

import asyncio
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import time
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from engines import engine_for
from engines.llama_cpp.gguf import facts, read_metadata
from loadcoach.host_memory import resident_mib
from loadcoach.models import at_context
from loadcoach.runner import ConfiguredRamCapacity, Lock, Refusal, Server, ServiceRamCapacity, Ticket

KEEP_SECONDS = 60  # a kept server is let go when its task has asked for nothing in this long


class Probes:
    """Run short-lived measurement requests under the ordinary model scheduler."""

    def __init__(self, runner, draining, allowed):
        """Bind native probe execution to the runner, model library and retained log."""
        self.runner, self.draining, self.allowed = runner, draining, allowed
        self.environment = {}
        self._engine_stamp = None
        self.working = {}
        self.working_since = {}
        self.kept = None  # a server and the GPU lock kept between the samples of one task that asked to reuse it
        self.candidate = None

    async def yield_to_foreground(self):
        """Finish isolated-process cleanup before an enabled wait limit estimates admission."""
        await self.drop_kept()
        lock = self.runner.holder()
        if lock and lock.caller == 'probe' and self.working:
            work = list(self.working.values())
            for task in work:
                if not task.cancelling():
                    task.cancel()
            await asyncio.shield(asyncio.gather(*work, return_exceptions=True))
            if self.runner.lock is lock:
                self.runner.lock = None
                self.runner.wake.set()

    async def identify(self, engine="llama_cpp") -> dict:
        """Fingerprint engine bytes plus machine/GPU identity, independently of model identity."""
        engine_adapter = engine_for(engine)
        installed = engine_adapter.installation(self.runner.config)
        if not installed.enabled:
            raise ValueError("llama.cpp is disabled on this compute node.")
        binary = Path(shutil.which(installed.executable) or installed.executable)
        stat = binary.stat()
        stamp = (engine, str(binary.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self.environment and stamp == self._engine_stamp:
            return self.environment
        def digest():
            """Hash the engine or model file used to identify probe evidence."""
            with binary.open('rb') as source:
                return hashlib.file_digest(source, 'sha256').hexdigest()
        engine_hash = await asyncio.to_thread(digest)
        if hasattr(engine_adapter, 'build_version'):
            build = await engine_adapter.build_version(self.runner.config)
            engine_hash = hashlib.sha256((engine_hash + build).encode()).hexdigest()
        try:
            cards = await self.runner.smi('--query-gpu=index,uuid,name,memory.total,driver_version')
        except (OSError, ValueError, TimeoutError):
            cards = []
        machine = {'host': platform.node(), 'system': platform.platform(), 'gpus': cards, 'assigned_devices': list(self.runner.config.gpus)}
        machine_id = hashlib.sha256(json.dumps(machine, sort_keys=True).encode()).hexdigest()
        self.environment = {'engine': await engine_adapter.version(installed.executable), 'engine_hash': engine_hash, 'machine': machine, 'machine_hash': machine_id}
        self._engine_stamp = stamp
        return self.environment

    def subject(self, model: str) -> tuple[str, dict]:
        """Freeze configured identity; probe overrides never mutate the library's model settings."""
        settings = self.runner.models.get(model)
        if settings is not None:
            adapter = engine_for(settings)
            if not (adapter.id == "llama_cpp" or adapter.capabilities(settings).get('load_probes')) or not adapter.installation(self.runner.config).enabled:
                raise ValueError("Native llama.cpp probes are unavailable for this model.")
        if settings is None or not Path(settings['flags']['model']).is_file():
            raise ValueError('The model file is not available.')
        multimedia = adapter.id in ('sdcpp', 'audiocpp')
        if not multimedia and not self.runner.needs(settings) and not settings['flags'].get('embedding'):
            raise ValueError('CPU probes require an embedding model.')
        mark = self.runner.models.fingerprint(settings, model)
        if not mark:
            raise ValueError('The model files are still being fingerprinted. Try again shortly.')
        metadata = {} if multimedia else read_metadata(settings['flags']['model'], count_parameters=True)
        values = {key: value for key, value in facts(metadata).items() if type(value) in (int, float)}
        values['configured_context'] = settings['flags']['ctx-size']
        # Some GGUF writers include the exact parameter count. Never guess it from quantised bytes.
        count = metadata.get('general.parameter_count')
        if type(count) in (int, float):
            values['parameter_count'] = count
        return mark, {'model_id': self.runner.models.identity(model), 'file_revision': self.runner.models.file_revision(model),
                      'settings': settings, 'values': values, 'metadata': facts(metadata), 'version': 1}

    def current_name(self, task: dict) -> str | None:
        """Resolve a task's pinned model again after awaits that can overlap a rename."""
        return self.runner.models.name_for(task['model_id']) if task.get('model_id') else task['model']

    def model_process(self, pid: int, process_root: Path = Path('/proc')) -> bool:
        """Recognize an outside inference engine or mapped model weights; desktop use is allowed."""
        process = process_root / str(pid)
        engines = {Path(self.runner.config.llama_server).name, 'llama-server', 'llama-cli', 'ollama', 'vllm'}
        try:
            if process.joinpath('exe').readlink().name.removesuffix(' (deleted)') in engines:
                return True
        except OSError:
            pass  # the process may have exited, or its executable may not be readable
        try:
            return any(re.search(r'\.(?:gguf|safetensors)(?: \(deleted\))?$', line, re.IGNORECASE)
                       for line in process.joinpath('maps').read_text(errors='replace').splitlines())
        except OSError:
            return False  # no evidence of a resident model; allocation size alone is not evidence

    def outside_processes(self, indexes: set[int], own_pid: int | None = None) -> str:
        """Identify other model processes on measured cards, allowing monitor and desktop use."""
        processes = []
        owned = {server.process.pid for server in self.runner.servers.values() if server.process}
        for pid, used in sorted(self.runner.seen['pids'].items()):
            cards = indexes & set(used)
            if pid != own_pid and cards and (pid in owned or self.model_process(pid)):
                allocation = ', '.join(f'GPU {card}: {used[card]} MiB' for card in sorted(cards))
                processes.append(f'PID {pid} ({allocation})')
        return '; '.join(processes)

    async def probe(self, request, task: dict, options: dict):
        """Reserve otherwise idle GPUs for one native probe and yield to foreground work."""
        kept = self.kept if self.kept and self.kept['task'] == task['id'] else None
        if self.kept and not kept:
            await self.drop_kept()  # another task's server is not ours to reuse
        if kept and (kept['server'].process is None or kept['server'].dead()
                     or self.runner.servers.get(kept['server'].model) is not kept['server']):
            await self.drop_kept()  # dispatch may have reaped it before the idle watcher ran
            kept = None
        if kept:
            kept['watch'].cancel()  # in use now; it is watched again when this sample ends
        elif self.draining() or self.runner.admitting or self.runner.running or self.runner.waiting or self.runner.loads or self.runner.held() or self.runner.holder():
            raise Refusal(503, 'Compute is busy; retry this probe later.')
        if kept and (self.draining() or self.runner.admitting or self.runner.waiting or self.runner.holder() is not kept['lock']):
            await self.drop_kept()
            raise Refusal(503, 'Compute is busy; retry this probe later.')
        lock = kept['lock'] if kept else Lock(secrets.token_hex(16), task['user'], 'probe', 86400, False, True)
        ticket = kept['ticket'] if kept else Ticket(None, task['user'], 'probe', 'low', wants=lock)
        work = None
        keeping = False
        self.kept = None
        try:
            if not kept:
                await self.runner.acquire(ticket)
            work = asyncio.create_task(self.sample(task, options, kept))
            self.working[task['id']] = work
            self.working_since[task['id']] = time.monotonic()
            while not work.done():
                await asyncio.sleep(.1)
                if (self.draining() or self.runner.admitting or self.runner.waiting or self.runner.holder() is not lock
                    or not self.allowed(task['id']) or await request.is_disconnected()):
                    if not work.cancelling():
                        work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise Refusal(409, 'Probe interrupted; retry the incomplete sample.')
            try:
                return await work
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                raise Refusal(409, 'Probe interrupted; retry the incomplete sample.') from None
        finally:
            if work:
                if not work.cancelling():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                self.working.pop(task['id'], None)
                self.working_since.pop(task['id'], None)
            held, self.candidate = self.candidate, None
            if held and options.get('reuse') and not work.cancelled() and work.exception() is None:
                # The server and the lock stay for the task's next sample, until it is idle, ends or ordinary work waits.
                self.kept = dict(task=task['id'], key=held[1], server=held[0], lock=lock, ticket=ticket, at=time.monotonic())
                self.kept['watch'] = asyncio.create_task(self.watch_kept(self.kept))
                keeping = True
            elif held:
                await self.runner.stop(held[0])
                self.runner.servers.pop(task['model'], None)
            if not keeping:
                self.runner.release(ticket)
                if self.runner.lock is lock:
                    self.runner.lock = None
                    self.runner.wake.set()

    async def drop_kept(self, task: str | None = None):
        """Stop a kept server and give its GPUs back; with a task, only if it is that task's."""
        kept = self.kept
        if not kept or task is not None and kept['task'] != task:
            return
        self.kept = None
        if kept['watch'] is not asyncio.current_task():
            kept['watch'].cancel()
        await self.runner.stop(kept['server'])
        if self.runner.servers.get(kept['server'].model) is kept['server']:
            del self.runner.servers[kept['server'].model]
        self.runner.release(kept['ticket'])
        if self.runner.lock is kept['lock']:
            self.runner.lock = None
            self.runner.wake.set()

    async def watch_kept(self, kept: dict):
        """Let go of a kept server when it has been idle, its task is over, or other work wants the GPUs."""
        while self.kept is kept:
            await asyncio.sleep(.25)
            if (self.draining() or self.runner.admitting or self.runner.waiting or self.runner.holder() is not kept['lock']
                    or not self.allowed(kept['task']) or time.monotonic() - kept['at'] > KEEP_SECONDS
                    or kept['server'].process is None or kept['server'].process.returncode is not None):
                await self.drop_kept()

    async def load_sample(self, task, options):
        """Measure image/audio native loading under the same probe lock and RAM admission."""
        if (options.get('context') or any(options.get(key) for key in ('inputs', 'outputs', 'warmup_outputs', 'reuse', 'embedding', 'warmup_embeddings'))
                or options.get('evaluation') is not None or options.get('dialogue') is not None):
            raise ValueError('Image and audio models support load-only measurements; token context is unavailable.')
        original = task['subject']['settings']
        current = self.current_name(task)
        if self.runner.models.fingerprint(self.runner.models.get(current), current or '') != task['fingerprint']:
            raise ValueError('Model fingerprint changed; discard this probe.')
        placement = self.runner.fit(current, original, set())
        if placement is None:
            raise Refusal(503, 'The model does not fit its node GPU/RAM admission limits; retry later.')
        settings, cards, evictions = placement
        if cards:
            await self.runner.look(fresh=True)
            if self.runner.seen is None:
                raise ValueError('GPU telemetry is unavailable; isolated loading cannot be verified.')
            indexes = {int(re.search(r'\d+$', card)[0]) for card in cards}
            if outside := self.outside_processes(indexes):
                raise Refusal(503, f'Waiting for another GPU model process to finish: {outside}.')
        for old in evictions:
            await self.runner.stop(old)
            self.runner.servers.pop(old.model, None)
        server = Server(current, tuple(cards), settings=settings, configured=original, health_interval=.02, probing=True)
        self.runner.servers[current] = server
        peak_gpu = 0
        async def observe():
            """Enforce the admitted memory budget and collect native loading peaks."""
            nonlocal peak_gpu
            if server.process and not server.dead():
                self.runner.sample_resident(server)
                self.runner.assert_cpu_memory(server, isolated_probe=True)
                if cards:
                    await self.runner.look(fresh=True)
                    if self.runner.seen is None:
                        raise ValueError('GPU telemetry is unavailable during model loading.')
                    peak_gpu = max(peak_gpu, sum(self.runner.uses(server) or []))
        async def watch():
            """Sample resource use until the isolated load has finished."""
            while True:
                await observe()
                await asyncio.sleep(.1)
        monitor = asyncio.create_task(watch())
        try:
            began = time.perf_counter()
            await self.runner.start(server)
            if original.get('engine') == 'sdcpp':
                loaded = await self.runner.client.post(server.url + '/load', headers=server.headers,
                                                       timeout=original['sdcpp'].get('timeout', 1800))
                loaded.raise_for_status()
            elapsed = time.perf_counter() - began
            await observe()
            if monitor.done():
                await monitor
            values = {'success': True, 'context_tokens': 0, 'context_available': False,
                      'load_seconds': elapsed, 'peak_gpu_mib': peak_gpu or None,
                      'peak_ram_mib': server.rss_peak_mib, 'cards': list(cards), 'device': 'gpu' if cards else 'cpu'}
            return {'values': values, 'output': '', 'log': server.output(),
                    'evidence': {'basis': 'native model readiness; no generation', 'context_available': False}}
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await self.runner.stop(server)
            if self.runner.servers.get(current) is server:
                self.runner.servers.pop(current)

    async def sample(self, task: dict, options: dict, kept: dict | None = None):
        """Run a fresh process, collect native timings and GPU telemetry, then stop it.

        With `reuse` the process is kept for the same task's next sample, which saves loading the model
        for every case of a quality benchmark. Each sample is still its own request with its prompt cache off.
        """
        original = task['subject']['settings']
        if engine_for(original).capabilities(original).get('load_probes'):
            return await self.load_sample(task, options)
        context, inputs, outputs = options['context'], options['inputs'], options['outputs']
        warmup_outputs = options['warmup_outputs']
        current = self.current_name(task)
        if self.runner.models.fingerprint(self.runner.models.get(current), current or '') != task['fingerprint']:
            raise ValueError('Model fingerprint changed; discard this probe.')
        needs = self.runner.needs(original)
        cpu = not needs
        if cpu and (not original['flags'].get('embedding') or not options.get('embedding')
                    or outputs or warmup_outputs or options.get('evaluation') is not None
                    or options.get('dialogue') is not None):
            raise ValueError('CPU probes support embedding inputs or embedding-model load only.')
        cards = tuple(needs if isinstance(needs, list) else self.runner.cards[:needs])
        if not set(cards) <= set(self.runner.cards) or (not isinstance(needs, list) and len(cards) != needs):
            raise ValueError("This model requires a disabled or unavailable card.")
        if not cpu:
            await self.runner.look(fresh=True)
            if self.runner.seen is None:
                raise ValueError('GPU telemetry is unavailable; isolated measurement cannot be verified.')
        settings = at_context(original, context)
        if original.get("gpus") == "any":
            placed = self.runner.fit(task["model"], {**settings, "min_context": context}, set())
            if placed is None:
                raise ValueError("This model cannot fit on the enabled cards.")
            settings, cards, _ = placed
        indexes = {int(re.search(r'\d+$', card)[0]) for card in cards}
        if not cpu and not kept and (outside := self.outside_processes(indexes)):
            # Outside model servers finish themselves. Desktop/monitor allocations are allowed.
            raise Refusal(503, f'Waiting for another GPU model process to finish: {outside}.')
        settings = {**settings, 'flags': {**settings['flags'], 'parallel': 1, 'ctx-size': context}}
        if not cpu and settings['flags'].get('no-kv-offload') and not options.get('embedding'):
            raise ValueError('GPU probes require KV offload; this model disables it.')
        layers = task['subject']['values'].get('layers')
        offload = settings['flags'].get('gpu-layers', settings['flags'].get('n-gpu-layers'))
        if not cpu and layers and offload not in ('all', -1, '-1') and (offload is None or int(offload) < layers):
            raise ValueError('GPU probes require all model layers offloaded; increase gpu-layers first.')
        if not cpu and os.environ.get('GGML_CUDA_ENABLE_UNIFIED_MEMORY', '0') not in ('', '0'):
            raise ValueError('Disable CUDA unified memory before GPU probes; it may spill allocations into host RAM.')
        key = json.dumps([task['id'], settings, sorted(cards)], sort_keys=True, default=str)
        if kept and kept['key'] != key:
            await self.runner.stop(kept['server'])
            self.runner.servers.pop(task['model'], None)
            kept = None
        if cpu and not kept:
            required = self.runner.ram_required(task['model'], settings)
            room = self.runner.host_memory()
            if room.get('service_available') == 0:
                raise Refusal(503, f"{task['model']} does not fit the available host/service RAM budget.", 10)
            total = room.get('total')
            fixed = [('configured_host_ram_limit', room.get('configured_limit') or self.runner.config.host_ram_limit_mib or None),
                     ('host_ram_after_headroom', max(0, total - self.runner.config.host_ram_headroom_mib) if total else None)]
            fixed = [(basis, limit) for basis, limit in fixed if limit is not None]
            if fixed and required > (smallest := min(limit for _, limit in fixed)):
                basis = next(basis for basis, limit in fixed if limit == smallest)
                prior = self.runner.models.resident_measure(task['model'], settings)
                reason = (f"{task['model']} cannot enter this CPU probe at {context} context tokens: "
                          f"its conservative {required} MiB admission bound exceeds the fixed "
                          f"{basis.replace('_', ' ')} of {smallest} MiB; no native load was attempted.")
                admission = {'basis': basis, 'required_mib': required, 'limit_mib': smallest,
                             'estimate_source': 'prior_same_context_rss' if prior is not None else 'model_file_bound',
                             'prior_peak_ram_mib': prior, 'native_attempted': False}
                return {'values': {'context_tokens': context, 'success': False, 'device': 'cpu',
                                   'cards': [], 'peak_gpu_mib': None, 'peak_ram_mib': None, 'rss_mib': None,
                                   'capacity_basis': 'fixed_admission_bound',
                                   'admission_required_mib': required, 'admission_limit_mib': smallest,
                                   'error': reason},
                        'output': '', 'log': '', 'evidence': {'admission': admission}}
            if required > room['free']:
                raise Refusal(503, f"{task['model']} does not fit the available host/service RAM budget.", 10)
        server = kept['server'] if kept else Server(task['model'], cards, settings=settings, configured=original, health_interval=.02)
        self.runner.servers[task['model']] = server
        values = {'context_tokens': context, 'success': False, 'peak_gpu_mib': None, 'cards': list(cards)}
        if cpu:
            values.update(device='cpu', peak_ram_mib=None, rss_mib=None)
        watch = None
        content = ''
        evidence = {}
        ok = False
        cpu_violation = [None]
        gpu_violation = [None]
        def observe_cpu():
            """Sample resident CPU memory while the native probe is active."""
            if not server.process or server.dead():
                return None
            rss = resident_mib(server.process.pid)
            if rss is None:
                return None
            server.rss_mib = rss
            if rss > (server.rss_peak_mib or 0):
                server.rss_peak_mib = rss
                self.runner.models.resident_measure(server.model, server.settings, rss)
            self.runner.assert_cpu_memory(server, isolated_probe=True)
            return rss
        try:
            if cpu:
                async def memory():
                    """Observe current host-memory usage during the active probe phase."""
                    while True:
                        try:
                            observe_cpu()
                        except Refusal as problem:
                            cpu_violation[0] = problem
                            await self.runner.stop(server)
                            return
                        await asyncio.sleep(.1)
                watch = asyncio.create_task(memory())  # include RAM used while the process loads
            began = time.perf_counter()
            if kept:
                values['server_reused'] = True
            else:
                await self.runner.start(server)
            values['load_seconds'] = time.perf_counter() - began
            server.probing = True  # report native readiness without admitting ordinary requests
            if cpu:
                if cpu_violation[0]:
                    raise cpu_violation[0]
                if observe_cpu() is None:
                    raise ValueError('CPU resident RAM could not be measured for the probe process.')
            else:
                offload = re.search(r'offloaded (\d+)/(\d+) layers', server.output())
                if offload and offload[1] != offload[2]:
                    raise ValueError('This run loaded model layers on the CPU. GPU-only probes require full offload.')
                await self.runner.look(fresh=True)
                peak = [sum(self.runner.uses(server) or [])]
                if not peak[0]:
                    raise ValueError('No GPU allocation was reported for this model process.')
                async def memory():
                    """Observe current host-memory usage during the active probe phase."""
                    while True:
                        await self.runner.look(fresh=True)
                        if self.runner.seen is None:
                            raise ValueError('GPU telemetry was lost during the measurement.')
                        peak[0] = max(peak[0], sum(self.runner.uses(server) or []))
                        if outside := self.outside_processes(indexes, server.process.pid):
                            gpu_violation[0] = Refusal(502, f'Another GPU model process appeared during measurement: '
                                                          f'{outside}; sample is not comparable.')
                            await self.runner.stop(server)
                            return
                        await asyncio.sleep(.5)
                watch = asyncio.create_task(memory())
            if options.get('dialogue') is not None or 'messages' in (options.get('evaluation') or {}):
                measured, content, evidence = await self.dialogue(server, options)
                values.update(measured)
                if options.get('evaluation') is not None:
                    content = evidence['native_message'].get('content') or ''
            elif options.get('evaluation') is not None:
                prompt, evidence = await self.evaluation_prompt(server, options)
                budget = min(outputs, context - len(prompt) - 32)  # leave native BOS/stop bookkeeping room
                if budget < 32:
                    raise ValueError('Evaluation prompt exceeds the available context with answer space.')
                sampling = self.evaluation_sampling(options['evaluation'])
                measured, content = await self.completion(server, prompt, budget, ignore_eos=False,
                    temperature=self.evaluation_temperature(options['evaluation']), sampling=sampling)
                values.update(measured, requested_input_tokens=inputs, output_budget=budget, context_guard_tokens=32)
                evidence['request_sampling'] = {'temperature':self.evaluation_temperature(options['evaluation']), **sampling}
                if measured['input_tokens'] != len(prompt):
                    raise ValueError('The server did not process the complete evaluation prompt.')
            elif inputs:
                tokenized = await self.runner.client.post(server.url + '/tokenize', json={'content': options['text'], 'add_special': True}, timeout=30)
                tokenized.raise_for_status()
                tokens = tokenized.json()['tokens']
                if not tokens or not all(type(token) is int for token in tokens):
                    raise ValueError('Tokenizer did not return a token sequence.')
                prompt = (tokens * (inputs // len(tokens) + 1))[:inputs]
                if options.get('embedding'):
                    if options.get('warmup_embeddings'):
                        await self.embedding(server, prompt)  # complete, unscored, stateless warm-up
                    measured, content, evidence = await self.embedding(server, prompt)
                    values.update(measured)
                else:
                    if warmup_outputs:
                        await self.completion(server, prompt, warmup_outputs)  # unscored warm-up, with cache reuse off
                    measured, content = await self.completion(server, prompt, outputs)
                    values.update(measured)
                    if measured['input_tokens'] != inputs or measured['output_tokens'] < 1:
                        raise ValueError('The server did not process the full prompt and generate an answer.')
            if watch.done():
                await watch
            if gpu_violation[0]:
                raise gpu_violation[0]
            if cpu:
                if cpu_violation[0]:
                    raise cpu_violation[0]
                rss = observe_cpu()
                if rss is None:
                    raise ValueError('CPU resident RAM could not be measured for the probe process.')
                values.update(peak_ram_mib=server.rss_peak_mib, rss_mib=rss)
                evidence['resource'] = {'device': 'cpu', 'peak_ram_mib': server.rss_peak_mib, 'rss_mib': rss}
            else:
                values['peak_gpu_mib'] = peak[0] or None
            values['success'] = not (values.get('unsupported') or values.get('unavailable'))
            ok = True
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            error = cpu_violation[0] or gpu_violation[0] or failure
            if isinstance(error, (ConfiguredRamCapacity, ServiceRamCapacity)):
                # A real process breached the configured budget or its own unit's
                # service ceiling. Context/Load can search smaller sizes.
                basis = ('measured_configured_ram' if isinstance(error, ConfiguredRamCapacity)
                         else 'measured_service_ram')
                values.update(error=str(error), capacity_basis=basis,
                              peak_ram_mib=server.rss_peak_mib, rss_mib=server.rss_mib)
                evidence['resource'] = {'device': 'cpu', 'peak_ram_mib': server.rss_peak_mib,
                                        'rss_mib': server.rss_mib}
                if isinstance(error, ServiceRamCapacity):
                    values.update(service_used_mib=error.used_mib, service_limit_mib=error.limit_mib)
                    evidence['service'] = {'scope': 'self', 'used_mib': error.used_mib,
                                           'limit_mib': error.limit_mib, 'native_attempted': True}
            elif isinstance(error, Refusal):
                raise error
            elif isinstance(error, httpx.TransportError):
                # A CUDA abort also closes HTTP. Drain its log before deciding whether to retry.
                await self.runner.stop(server)
                if server.listener:
                    await asyncio.wait({server.listener}, timeout=2)
            if not isinstance(error, (ConfiguredRamCapacity, ServiceRamCapacity)):
                reason = self.capacity_error(error, server.output())
                if reason:
                    values['error'] = reason
                elif isinstance(error, httpx.TransportError):
                    raise Refusal(502, f'Model connection interrupted ({type(error).__name__}): '
                                  f'{str(error) or "no response"}. Last server output: {server.output()[-2000:]}') from error
                else:
                    raise
        finally:
            if watch:
                # A measured RAM breach or outside GPU process already asked its watcher
                # to stop our child. Let that stop finish before final cleanup below.
                if not (cpu_violation[0] or gpu_violation[0]):
                    watch.cancel()
                await asyncio.gather(watch, return_exceptions=True)
            if not (ok and options.get('reuse')):
                await self.runner.stop(server)
                if self.runner.servers.get(task['model']) is server:
                    del self.runner.servers[task['model']]
        current = self.current_name(task)
        if self.runner.models.fingerprint(self.runner.models.get(current), current or '') != task['fingerprint']:
            if ok and options.get('reuse'):
                await self.runner.stop(server)
                self.runner.servers.pop(task['model'], None)
            raise ValueError('Model fingerprint changed during the sample; the sample was discarded.')
        if ok and options.get('reuse'):
            self.candidate = (server, key)
        return {'values': values, 'output': content if options.get('evaluation') is not None or options.get('dialogue') is not None else content[:32000],
                'log': server.output()[-16000:], 'evidence': evidence}

    async def embedding(self, server: Server, prompt: list[int]):
        """Time a single pooled vector, requiring proof that the entire input was processed."""
        body = {'input': prompt, 'encoding_format': 'float', 'cache_prompt': False}
        began = time.perf_counter()
        response = await self.runner.client.post(server.url + '/v1/embeddings', json=body,
                                                timeout=httpx.Timeout(600, connect=5,
                                                                      read=(3300 if len(prompt) > 8192 else 1700)
                                                                      if not server.cards else 600))
        elapsed = time.perf_counter() - began
        if response.status_code != 200:
            raise ValueError(response.text[:2000])
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get('usage'), dict):
            raise ValueError('The embedding response needs native usage counts.')
        usage, data = result.get('usage', {}), result.get('data')
        timings = result.get('timings', {})
        if (type(usage.get('prompt_tokens')) is not int or usage['prompt_tokens'] != len(prompt)
            or not isinstance(timings, dict) or timings.get('cache_n', 0) or result.get('truncated')):
            raise ValueError('The embedding response did not report the complete uncached input.')
        if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
            or type(data[0].get('index')) is not int or data[0]['index'] != 0):
            raise ValueError('Expected one pooled embedding for the single input.')
        vector = data[0].get('embedding')
        if (not isinstance(vector, list) or not vector or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in vector) or
            not any(vector)):
            raise ValueError('The embedding must be a finite, nonzero pooled vector; check pooling settings.')
        if elapsed <= 0:
            raise ValueError('No positive embedding request duration was measured.')
        values = {'input_tokens': usage['prompt_tokens'], 'output_tokens': 0, 'request_seconds': elapsed,
                  'embedding_tokens_per_second': len(prompt)/elapsed, 'embeddings_per_second': 1/elapsed,
                  'embedding_dimensions': len(vector)}
        evidence = {'endpoint': '/v1/embeddings', 'usage': usage, 'cache_prompt': False,
                    'prompt_tokens_sha256': hashlib.sha256(json.dumps(prompt).encode()).hexdigest(),
                    'embedding_sha256': hashlib.sha256(json.dumps(vector).encode()).hexdigest(),
                    'timing': 'local request including vector response', 'embedding_dimensions': len(vector)}
        return values, json.dumps({'dimensions': len(vector), 'usage': usage}), evidence

    @staticmethod
    def evaluation_temperature(spec):
        """Keep deterministic zero as the default; allow bounded seeded sampler diagnostics."""
        value = spec.get('temperature', 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('Evaluation temperature must be a finite number from 0 to 1.')
        return value

    @staticmethod
    def evaluation_sampling(spec):
        """Allow only bounded native sampling controls for isolated diagnostic evaluations."""
        sampling = spec.get('sampling', {})
        bounds = {'top_p':(0,1), 'min_p':(0,1), 'repeat_penalty':(.5,2),
                  'presence_penalty':(-2,2), 'frequency_penalty':(-2,2)}
        if not isinstance(sampling,dict) or sampling.keys()-({*bounds,'top_k'}):
            raise ValueError('Evaluation sampling has unsupported controls.')
        for name,value in sampling.items():
            if name == 'top_k':
                if type(value) is not int or not 0 <= value <= 1000:
                    raise ValueError('Evaluation top_k must be a whole number from 0 to 1000.')
            elif type(value) not in (int,float) or not math.isfinite(value) or not bounds[name][0] <= value <= bounds[name][1]:
                raise ValueError(f'Evaluation {name} is outside its bounded range.')
        return sampling

    @staticmethod
    def validate_response_format(value):
        """Accept bounded, self-contained JSON formats without remote/cyclic schema references."""
        if not isinstance(value,dict):
            raise ValueError('response_format must be a JSON object.')
        if value == {'type':'json_object'}:
            return
        if value.keys() != {'type','json_schema'} or value.get('type') != 'json_schema':
            raise ValueError('Use json_object or a named json_schema response_format.')
        spec = value['json_schema']
        if (not isinstance(spec,dict) or spec.keys()-{'name','schema','strict'} or 'schema' not in spec
                or not isinstance(spec.get('name'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',spec['name'])
                or not isinstance(spec['schema'],dict) or type(spec.get('strict',False)) is not bool):
            raise ValueError('json_schema needs a name, a schema object and an optional strict boolean.')
        def bounded(item,depth=0):
            """Send the response-format qualification request within its token limits."""
            if depth > 24:
                raise ValueError('Response schema nesting is too deep.')
            if isinstance(item,dict):
                if set(item) & {'$ref','$dynamicRef','$recursiveRef'}:
                    raise ValueError('Response schemas must be self-contained without references.')
                for child in item.values():
                    bounded(child,depth+1)
            elif isinstance(item,list):
                for child in item:
                    bounded(child,depth+1)
        bounded(spec['schema'])
        if len(json.dumps(value,allow_nan=False).encode()) > 16384:
            raise ValueError('Response formats must be at most 16 KiB.')
        try:
            Draft202012Validator.check_schema(spec['schema'])
        except SchemaError as problem:
            raise ValueError('Invalid response schema: '+problem.message[:500]) from None

    @staticmethod
    def validate_dialogue(spec):
        """Accept text-only native conversations, not media paths, code or endpoint overrides."""
        if not isinstance(spec,dict) or 'messages' not in spec or spec.keys()-{'messages','tools','response_format','temperature','sampling'}:
            raise ValueError('A conversation contains messages and optional tools, response_format or sampling.')
        Probes.evaluation_temperature(spec)
        Probes.evaluation_sampling(spec)
        messages, tools = spec['messages'],spec.get('tools',[])
        if not isinstance(messages,list) or not 1 <= len(messages) <= 40:
            raise ValueError('A conversation needs one to 40 messages.')
        try:
            encoded = json.dumps(spec,allow_nan=False).encode()
        except RecursionError:
            raise ValueError('Conversation nesting is too deep.') from None
        if len(encoded) > 524288:
            raise ValueError('A conversation must be at most 512 KiB.')
        for message in messages:
            if (not isinstance(message,dict) or message.keys()-{'role','content','tool_calls','tool_call_id'}
                    or message.get('role') not in ('system','user','assistant','tool')
                    or message.get('content') is not None and not isinstance(message['content'],str)):
                raise ValueError('Conversation messages must contain text only.')
            role = message['role']
            calls = message.get('tool_calls',[])
            if not isinstance(calls,list) or len(calls)>12 or calls and role!='assistant':
                raise ValueError('Only assistant messages may contain up to twelve function calls.')
            if not isinstance(message.get('content'),str) and not (role=='assistant' and calls):
                raise ValueError('Messages need text, or assistant function calls.')
            if role=='tool' and (not isinstance(message.get('tool_call_id'),str) or not message['tool_call_id']):
                raise ValueError('Tool results need a tool_call_id.')
            if 'tool_call_id' in message and role!='tool':
                raise ValueError('Only tool results may have a tool_call_id.')
            for call in calls:
                if (not isinstance(call,dict) or call.get('type')!='function' or not isinstance(call.get('id'),str)
                        or not isinstance(call.get('function'),dict) or not isinstance(call['function'].get('name'),str)
                        or not isinstance(call['function'].get('arguments'),str)):
                    raise ValueError('Malformed native function call.')
        if not isinstance(tools,list) or len(tools)>16:
            raise ValueError('A conversation accepts at most sixteen function descriptions.')
        for tool in tools:
            function = tool.get('function') if isinstance(tool,dict) else None
            if (not isinstance(function,dict) or tool.get('type')!='function'
                    or not isinstance(function.get('name'),str) or not function['name']
                    or not isinstance(function.get('parameters'),dict)):
                raise ValueError('Tools need a function name and parameter schema.')
        if 'response_format' in spec:
            Probes.validate_response_format(spec['response_format'])

    async def dialogue(self, server, options):
        """Measure a native conversation; callers alone run controlled tools and graders."""
        spec = options.get('dialogue') or options['evaluation']
        tools = spec.get('tools',[])
        general = options.get('evaluation') is not None or not tools or 'response_format' in spec
        body = {'messages':spec['messages']}
        if tools:
            body.update(tools=tools,tool_choice='auto',parallel_tool_calls=False)
        if 'response_format' in spec:
            body['response_format'] = spec['response_format']
        evidence = {'chat_template':True,'thinking':'as served','native_tools':bool(tools),
                    'native_dialogue':True,'response_format':spec.get('response_format'),
                    'request_sampling':{'temperature':self.evaluation_temperature(spec), **self.evaluation_sampling(spec)},
                    'native_message':{'role':'assistant','content':None}}
        def unavailable(stage,detail,unsupported=True):
            """Build an unsupported probe result without assigning a numeric score."""
            evidence.update(unavailable_stage=stage,native_error=detail[:2000])
            measured = {'unavailable':True,'unsupported':unsupported,'error':detail[:2000]}
            if 'prompt_token_count' in evidence:
                measured['prepared_input_tokens'] = evidence['prompt_token_count']
            return measured,json.dumps(evidence['native_message']),evidence
        rendered = await self.runner.client.post(server.url+'/apply-template',json=body,timeout=30)
        if general and rendered.status_code in (400,404,422,501):
            return unavailable('template','Native conversation template request rejected: '+rendered.text)
        rendered.raise_for_status()
        rendered_body = rendered.json()
        prompt = rendered_body.get('prompt') if isinstance(rendered_body,dict) else None
        if not isinstance(prompt,str):
            raise ValueError('Native conversation template did not return a text prompt.')
        evidence['prompt'] = prompt
        tokenized = await self.runner.client.post(server.url+'/tokenize',
            json={'content':prompt,'add_special':False,'parse_special':True},timeout=30)
        tokenized.raise_for_status()
        token_body = tokenized.json()
        tokens = token_body.get('tokens') if isinstance(token_body,dict) else None
        if not isinstance(tokens,list) or not tokens or not all(type(t) is int for t in tokens):
            raise ValueError('Tokenizer did not return token IDs for the conversation.')
        evidence['prompt_tokens_sha256'] = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
        evidence['prompt_token_count'] = len(tokens)
        budget = min(options['outputs'],options['context']-len(tokens)-32)
        if budget < 32:
            if general:
                return unavailable('context','Conversation exceeds the available context with answer space.',False)
            raise ValueError('Tool dialogue exceeds the available context with answer space.')
        body.update(max_tokens=budget,temperature=self.evaluation_temperature(spec),seed=42,stream=False,cache_prompt=False,
                    **self.evaluation_sampling(spec))
        if server.settings.get('adapters'):
            body['lora'] = [{'id':index,'scale':float(adapter.get('scale',1))} for index,adapter in enumerate(server.settings['adapters'].values())]
        began = time.perf_counter()
        response = await self.runner.client.post(server.url+'/v1/chat/completions',json=body,timeout=httpx.Timeout(3600,connect=5))
        if general and response.status_code in (400,404,422,501):
            return unavailable('generation','Native conversation/format request rejected: '+response.text)
        if response.status_code == 500 and 'does not match the expected' in response.text:
            # The native server could not parse the model's own output, such as a malformed tool call: that is a wrong
            # answer for this sample, kept with its reason, not a broken probe that ends the whole run.
            return unavailable('generation','The model produced output the native server could not parse: '+response.text,False)
        if response.status_code != 200:
            raise ValueError(response.text[:1000])
        result = response.json()
        if not isinstance(result,dict):
            raise ValueError('Native conversation response is not an object.')
        json.dumps(result,allow_nan=False)
        usage, timing, choices = result.get('usage',{}),result.get('timings',{}),result.get('choices')
        if (not isinstance(usage,dict) or not isinstance(timing,dict) or not isinstance(choices,list) or len(choices)!=1
                or not isinstance(choices[0],dict) or not isinstance(choices[0].get('message'),dict)
                or type(usage.get('prompt_tokens')) is not int or usage['prompt_tokens']!=len(tokens)
                or type(usage.get('completion_tokens')) is not int or not 0<=usage['completion_tokens']<=budget
                or timing.get('cache_n',0)
                or general and (type(timing.get('cache_n')) is not int or timing['cache_n']!=0)):
            raise ValueError('Native conversation response lacks complete uncached token counts or an assistant message.')
        message = choices[0]['message']
        if message.get('role')!='assistant' or message.get('content') is not None and not isinstance(message['content'],str):
            raise ValueError('Native conversation response is not a text assistant message.')
        calls = message.get('tool_calls',[])
        if not isinstance(calls,list) or any(not isinstance(c,dict) or not isinstance(c.get('function'),dict) for c in calls):
            raise ValueError('Native conversation response has malformed tool_calls.')
        evidence.update(native_message=message,native_usage=usage,native_timings=timing)
        details = usage.get('completion_tokens_details')
        reasoning = details.get('reasoning_tokens') if isinstance(details,dict) else None
        reported = ({'reasoning_tokens':reasoning} if type(reasoning) is int and 0<=reasoning<=usage['completion_tokens'] else {})
        return {**reported,'input_tokens':usage['prompt_tokens'],'output_tokens':usage['completion_tokens'],
                'output_budget':budget,'context_guard_tokens':32,'request_seconds':time.perf_counter()-began,
                'stop_type':choices[0].get('finish_reason'),'generation_capped':choices[0].get('finish_reason')=='length'}, json.dumps(message),evidence

    async def evaluation_prompt(self, server: Server, options: dict) -> tuple[list[int], dict]:
        """Apply the model's chat template and pad only between intact evidence blocks.

        The template is applied as it is for anyone who asks this model: nothing here switches its
        thinking on or off. What the model's own settings say about thinking holds.

        Each fixed fragment is tokenized once. Filler tokens fill equally spaced gaps to an
        exact requested input size, including template tokens. No question or fact is truncated.
        With inputs=0, send the unpadded question exactly as given (for standard datasets).
        """
        spec = options['evaluation']
        marker = 'BENCHMARK_GAP_' + secrets.token_hex(16)
        blocks = spec.get('blocks', [])
        user = (marker + ('\n' + marker).join('\n' + block + '\n' for block in blocks) + marker + '\n\n' if blocks else '') + spec['question']
        response = await self.runner.client.post(server.url + '/apply-template',
            json={'messages': [{'role': 'user', 'content': user}]}, timeout=30)
        response.raise_for_status()
        rendered = response.json()['prompt']
        async def tokenize(text, special=True):
            """Ask the selected native engine to tokenize the evaluation prompt."""
            response = await self.runner.client.post(server.url + '/tokenize',
                json={'content': text, 'add_special': False, 'parse_special': special}, timeout=30)
            response.raise_for_status()
            tokens = response.json()['tokens']
            if not all(type(token) is int for token in tokens):
                raise ValueError('Tokenizer did not return token IDs.')
            return tokens
        parts = [await tokenize(part) for part in rendered.split(marker)]
        base = sum(map(len, parts))
        target = options['inputs'] or base
        if target < base:
            raise ValueError('Evaluation input exceeds the available context input budget.')
        gaps = len(parts) - 1
        if target > base and not gaps:
            raise ValueError('Padding needs evidence blocks; exact prompts must use inputs=0.')
        padding = await tokenize(spec.get('filler', ''), False) if target > base else []
        if target > base and not padding:
            raise ValueError('Padding needs nonempty filler text.')
        prompt, positions = [], []
        for index, part in enumerate(parts):
            positions.append(len(prompt))
            prompt.extend(part)
            if index < gaps:
                count = (target-base)//gaps + (index < (target-base)%gaps)
                prompt.extend((padding * (count//len(padding)+1))[:count] if count else [])
        response = await self.runner.client.post(server.url + '/detokenize', json={'tokens': prompt}, timeout=30)
        response.raise_for_status()
        return prompt, {'prompt': response.json()['content'], 'prompt_tokens_sha256': hashlib.sha256(json.dumps(prompt).encode()).hexdigest(),
                        'fixed_fragment_token_offsets': positions, 'chat_template': True, 'thinking': 'as served'}

    @staticmethod
    def capacity_error(error: Exception, output: str) -> str | None:
        """Return explicit allocation/context evidence; unrelated CUDA errors are not a fit limit."""
        markers = ('out of memory', 'cudamalloc', 'exceeds the available context',
                   'exceed_context_size', 'failed to allocate',
                   'is larger than the max context size', 'is too large to process. increase the physical batch',
                   'cuda error: the resource allocation failed', 'cublas_status_alloc_failed')
        for line in (str(error) + '\n' + output).splitlines():
            if any(marker in line.lower() for marker in markers):
                return line.strip()[:2000]
        return None

    async def completion(self, server: Server, prompt: list[int], output: int, *, ignore_eos=True,
                         temperature=0, sampling=None) -> tuple[dict, str]:
        """Measure first emitted token at the client and use the server's actual token timings."""
        body = {'prompt': prompt, 'n_predict': output, 'temperature': temperature, 'seed': 42, 'stream': True,
                'cache_prompt': False, 'ignore_eos': ignore_eos, 'return_tokens': True}
        body.update(sampling or {})
        if server.settings.get('adapters'):
            body['lora'] = [{'id': index, 'scale': float(adapter.get('scale', 1))} for index, adapter in enumerate(server.settings['adapters'].values())]
        began, first, text, timing, stopped = time.perf_counter(), None, '', None, None
        async with self.runner.client.stream('POST', server.url + '/completion', json=body, timeout=httpx.Timeout(600, connect=5)) as response:
            if response.status_code != 200:
                raise ValueError((await response.aread()).decode(errors='replace')[:1000])
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                piece = json.loads(line[5:])
                if piece.get('error'):
                    raise ValueError(str(piece['error']))
                if first is None and (piece.get('content') or piece.get('tokens')):
                    first = time.perf_counter() - began
                text += piece.get('content', '')
                if piece.get('timings'):
                    timing = piece['timings']
                if piece.get('stop'):
                    stopped = piece.get('stop_type')
                if piece.get('truncated'):
                    raise ValueError('The server truncated the prompt.')
        if (first is None and ignore_eos) or timing is None or timing.get('cache_n', 0):
            raise ValueError('The response did not provide uncached token timings and an emitted token.')
        return {'input_tokens': timing['prompt_n'], 'output_tokens': timing['predicted_n'],
                'prompt_tokens_per_second': timing['prompt_per_second'], 'generation_tokens_per_second': timing['predicted_per_second'],
                'ttft_seconds': first, 'request_seconds': time.perf_counter() - began, 'stop_type': stopped,
                'generation_capped': stopped == 'limit'}, text
