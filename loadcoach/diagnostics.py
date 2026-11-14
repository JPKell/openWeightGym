"""Request timing evidence, without prompts, replies, tool arguments or credentials.

Compute measures monotonic intervals; control node presents the retained evidence. Native server
timings overlap those intervals and are never stacked into a fabricated wall-clock timeline.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

from loadcoach.usage import ENDINGS

PHASES = {
    'queue': 'Queue / admission',
    'loading': 'Waiting for model readiness',
    'upstream': 'Sending request / waiting for headers',
    'response': 'Reading response / waiting for generated output',
    'generation': 'Receiving generated output',
}
LIMIT = 1000


def number(value):
    """Only finite, nonnegative numbers are timing evidence; booleans are not numbers."""
    return value if type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value) else None


class Timeline:
    """One bounded, volatile observation, saved only in a logged request's ending."""

    def __init__(self, asked: dict, settings: dict | None, hosted: bool):
        """Start monotonic request timing with bounded metadata and phase state."""
        self.began = time.monotonic()
        self.finished = None
        self.phases = []
        self.first_byte = self.first_output = None
        self.native = {}
        self.tools = {}
        self.pending = b''
        self.discarding = False
        self.truncated = False
        from loadcoach.observability import CURRENT
        self.observation = CURRENT.get()
        self.observation_phase = None
        messages = asked.get('messages')
        messages = messages if isinstance(messages, list) else []
        self.context = {
            'source': 'hosted' if hosted else 'local',
            'images': any(isinstance(p, dict) and p.get('type') == 'image_url'
                          for m in messages if isinstance(m, dict) and isinstance(m.get('content'), list)
                          for p in m['content']),
            'tools_offered': len(asked['tools']) if isinstance(asked.get('tools'), list) else 0,
            'stream_requested': asked.get('stream') is True,
        }
        self.configure(settings)
        self.stage('upstream' if hosted else 'queue')

    def configure(self, settings: dict | None, cards=None):
        """Keep only settings useful for diagnostics, never arbitrary flags or paths."""
        if settings is not None:
            flags = settings.get('flags', {})
            self.context.update(projector=Path(flags['mmproj']).name if flags.get('mmproj') else None,
                                vision_configured=bool(flags.get('mmproj')), context_tokens=flags.get('ctx-size'))
        if cards is not None:
            self.context['cards'] = list(cards)

    def stage(self, name: str):
        """Close the previous timing interval and open a bounded diagnostic stage."""
        now = time.monotonic() - self.began
        if self.finished is not None or (self.phases and self.phases[-1]['stage'] == name):
            return
        from loadcoach.observability import Context, finish
        if self.observation_phase:
            finish(self.observation_phase)
        if self.observation:
            self.observation_phase = Context(self.observation.root, self.observation.service,
                                             name, self.observation.sink, self.observation.span_id,
                                             ingress=False)
        if self.phases:
            self.phases[-1]['seconds'] = now - self.phases[-1]['offset']
        if len(self.phases) < 128:
            self.phases.append({'stage': name, 'offset': now})
        else:
            self.truncated = True

    def document(self, document):
        """Read structured provider timing fields, not similarly named text in an answer."""
        if not isinstance(document, dict):
            return
        timings = document.get('timings')
        if isinstance(timings, dict):
            for key, target in (('prompt_ms', 'prompt_seconds'), ('predicted_ms', 'generation_seconds')):
                if (value := number(timings.get(key))) is not None:
                    self.native[target] = value / 1000
            if (value := number(timings.get('prompt_per_second'))) is not None and value > 0:
                self.native['prompt_tokens_per_second'] = value
        choices = document.get('choices')
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            message = choice.get('delta') or choice.get('message') or {}
            calls = message.get('tool_calls') if isinstance(message, dict) else None
            for position, call in enumerate(calls if isinstance(calls, list) else []):
                if not isinstance(call, dict):
                    continue
                index = call.get('index', position)
                function = call.get('function')
                if type(index) is not int or not 0 <= index < 64 or not isinstance(function, dict):
                    continue
                name = function.get('name')
                if isinstance(name, str) and name:
                    tool = self.tools.setdefault(index, {'tool': '', 'offset': time.monotonic() - self.began})
                    tool['tool'] = (tool['tool'] + name)[:256]

    def chunk(self, chunk: bytes, content_type: str, readable: bool):
        """Observe complete SSE data lines across arbitrary HTTP chunk boundaries.

        A role-only event, heartbeat, usage or error is not generated output. Reasoning and
        tool-call deltas count. Ordinary JSON cannot reveal when its first token was produced.
        Lines over 1 MiB are discarded without changing the forwarded response.
        """
        if chunk and self.first_byte is None:
            self.first_byte = time.monotonic() - self.began
        if not readable or 'text/event-stream' not in content_type:
            return
        pieces = (self.pending + chunk).split(b'\n')
        self.pending = pieces.pop()
        for line in pieces:
            if self.discarding:
                self.discarding = False
                continue
            if len(line) > 1024 * 1024 or not line.startswith(b'data:'):
                continue
            try:
                document = json.loads(line[5:])
            except (ValueError, UnicodeDecodeError):
                continue
            self.document(document)
            if not isinstance(document, dict) or not isinstance(document.get('choices'), list):
                continue
            for choice in document['choices']:
                delta = choice.get('delta') if isinstance(choice, dict) else None
                if isinstance(delta, dict) and any(delta.get(key) for key in ('content', 'reasoning_content', 'reasoning', 'tool_calls')):
                    if self.first_output is None:
                        self.first_output = time.monotonic() - self.began
                        self.stage('generation')
        if len(self.pending) > 1024 * 1024:
            self.pending = b''
            self.discarding = True

    def finish(self, raw: bytes = b'', content_type: str = '', readable: bool = True):
        """Capture permitted final timing evidence and end the request timeline."""
        if readable and 'application/json' in content_type:
            try:
                self.document(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                pass
        self.finished = time.monotonic() - self.began
        from loadcoach.observability import finish
        if self.observation_phase:
            finish(self.observation_phase)
            self.observation_phase = None
        if self.observation:
            self.observation.metadata.update({key: value for key, value in
                (('first_output_seconds', self.first_output),
                 ('queue_seconds', sum(p.get('seconds', 0) for p in self.phases if p['stage'] == 'queue')),
                 ('readiness_seconds', sum(p.get('seconds', 0) for p in self.phases if p['stage'] == 'loading')))
                if value is not None})
        self.pending = b''

    def snapshot(self):
        """Return current numeric timing evidence without request or answer text."""
        total = self.finished if self.finished is not None else time.monotonic() - self.began
        return {'version': 1, 'total_seconds': round(total, 6), 'context': self.context,
                'first_byte_seconds': self.first_byte, 'first_output_seconds': self.first_output,
                'native': self.native, 'truncated': self.truncated,
                'tools': list(self.tools.values()),
                'phases': [{**p, 'seconds': round(p.get('seconds', total - p['offset']), 6)} for p in self.phases]}


def presentation(asked: dict, ended: dict | None, live: dict | None = None):
    """Make old and new evidence comparable without pretending missing measurements exist."""
    evidence = (ended or {}).get('diagnostics') or live or {}
    phases = []
    for p in evidence.get('phases', []):
        if p.get('stage') in PHASES and number(p.get('seconds')) is not None:
            phases.append({**p, 'label': PHASES[p['stage']], 'basis': 'Measured on compute'})
    total = number(evidence.get('total_seconds'))
    total_basis = 'Measured on compute'
    if total is None and ended:
        try:
            total = max(0, (datetime.fromisoformat(ended['at']) - datetime.fromisoformat(asked['at'])).total_seconds())
        except (ValueError, KeyError):
            pass
        total_basis = 'Inferred from event timestamps (one-second precision)'
    native = evidence.get('native', {})
    metrics = [{'label': 'Total request', 'seconds': total, 'basis': total_basis},
               {'label': 'First response byte', 'seconds': number(evidence.get('first_byte_seconds')), 'basis': 'Observed on compute; includes queue and loading'},
               {'label': 'First generated output', 'seconds': number(evidence.get('first_output_seconds')), 'basis': 'Observed streamed content, reasoning or tool delta; includes queue and loading'},
               {'label': 'Prompt processing', 'seconds': number(native.get('prompt_seconds')), 'basis': 'Reported by upstream; overlaps the wall-clock stages'},
               {'label': 'Native generation', 'seconds': number(native.get('generation_seconds')), 'basis': 'Reported by upstream; overlaps the wall-clock stages'}]
    if not evidence:
        for key, label in (('waited', 'Admission including loading'), ('ran', 'Upstream request')):
            if (value := number((ended or {}).get(key))) is not None:
                phases.append({'label': label, 'seconds': value, 'basis': 'Legacy measurement; queue and loading were not separated'})
    return {'available': bool(evidence), 'phases': phases, 'metrics': metrics, 'context': evidence.get('context', {}),
            'tools': evidence.get('tools', []),
            'truncated': evidence.get('truncated', False), 'live': ended is None}


def related(log, asked: dict, ended: dict | None, operator: bool):
    """Correlate metadata only. A shared trace is correlation, not proof of parentage."""
    owner = None if operator else asked['user']
    traced = log.read(limit=201, user=owner, trace=asked['trace']) if asked.get('trace') else []
    # Model lifecycle output is shared operator evidence, not the caller's private text.
    models = []
    if operator and asked.get('model'):
        moment = datetime.fromisoformat(asked['at']).timestamp()
        until = datetime.fromisoformat(ended['at']).timestamp() + 1 if ended else time.time()
        models = log.read(limit=51, kind='model', model=asked['model'], since=moment - 1, until=until)
    fields = ('id', 'source', 'at', 'kind', 'what', 'model', 'tool', 'of', 'took', 'why')
    clean = lambda rows: [{k: row[k] for k in fields if k in row} for row in rows]
    trace_rows, model_rows = clean(traced[-200:]), clean(models[-50:])
    return {'trace': trace_rows, 'models': model_rows,
            'events': sorted({r['id']: r for r in trace_rows + model_rows}.values(), key=lambda row: (row['at'], row['id'])),
            'truncated': len(traced) > 200 or len(models) > 50}


def distribution(log, asked: dict, ended: dict | None, owner: str | None):
    """Bounded, authorized recent latency/error distributions; no model text is read."""
    marks = ','.join('?' for _ in ENDINGS)
    scope = ' AND user = ?' if owner is not None else ''
    rows = log.db.execute(f"SELECT model, detail FROM events WHERE kind = 'request' AND what IN ({marks}){scope} ORDER BY id DESC LIMIT ?",
                          (*ENDINGS, *((owner,) if owner is not None else ()), LIMIT + 1)).fetchall()
    groups = {}
    for model, raw in rows[:LIMIT]:
        entry = json.loads(raw)
        if entry.get('reliability_outcome') == 'benchmark':
            continue
        role = entry.get('diagnostics', {}).get('role') or entry.get('role') or ''
        fingerprint = entry.get('fingerprint') or entry.get('reliability_fingerprint') or ''
        for dimension, value, wanted in (('model', model, asked.get('model')), ('role', role, asked.get('role') or ''),
                                         ('fingerprint', fingerprint, (ended or {}).get('fingerprint') or (ended or {}).get('reliability_fingerprint') or '') ):
            if not value or value != wanted:
                continue
            group = groups.setdefault(dimension, {'dimension': dimension, 'value': value, 'requests': 0, 'errors': 0, 'latencies': []})
            group['requests'] += 1
            if entry.get('reliability_outcome') in ('error', 'timeout', 'restart') or entry.get('status', 0) >= 500:
                group['errors'] += 1
            latency = number(entry.get('diagnostics', {}).get('total_seconds'))
            if latency is not None:
                group['latencies'].append(latency)
    result = []
    for group in groups.values():
        values = sorted(group.pop('latencies'))
        result.append({**group, 'measured': len(values), 'p50': values[math.ceil(len(values) * .5) - 1] if values else None,
                       'p95': values[math.ceil(len(values) * .95) - 1] if values else None,
                       'error_percent': round(group['errors'] * 100 / group['requests'], 1)})
    return {'groups': result, 'examined': min(len(rows), LIMIT), 'limit': LIMIT, 'truncated': len(rows) > LIMIT,
            'scope': 'All authorized users' if owner is None else 'Your requests only'}
