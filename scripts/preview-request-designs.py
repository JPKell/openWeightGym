"""Render three paired Requests / individual request proposals using portable sample data."""

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def samples():
    records = []
    models = [('qwen3-32b', 'LoadCoach', '/v1/chat/completions'),
              ('qwen3-coder-30b', 'LoadCoach', '/v1/chat/completions'),
              ('nomic-embed', 'LoadCoach', '/v1/embeddings'),
              ('sdxl-studio', 'MirrorWall', '/v1/images/generations'),
              ('qwen3-tts', 'SoundRoom', '/v1/audio/speech')]
    states = ['Failed', 'Running', 'Queued', 'Done', 'Done', 'Done', 'Timeout', 'Done',
              'Cancelled', 'Done', 'Refused', 'No ending']
    now = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)
    for i in range(72):
        model, service, path = models[i % len(models)]
        state = states[i % len(states)] if i < 24 else ('Failed' if i % 19 == 0 else 'Done')
        node = ['atlas', 'orion', 'lyra'][i % 3]
        wait = round(.08 + (i % 8) * .41, 3)
        load = 6.82 if i % 13 == 0 else 0
        prefill = round(.35 + (i % 6) * .19, 3)
        generation = round(2.1 + (i % 9) * .67, 3)
        total = round(wait + load + prefill + generation, 3)
        if state == 'Timeout':
            wait, load, prefill, generation, total = 25.0, 0, 0, 5.0, 30.0
        if state in ('Refused', 'Queued'):
            wait, load, prefill, generation, total = .13, 0, 0, 0, .13
        if state == 'Failed':
            load, prefill, generation = 6.82, 0, 0
            total = round(wait + load, 3)
        phases = [dict(label=label, seconds=seconds, basis='Measured on compute')
                  for label, seconds in [('Admission / queue', wait), ('Model loading', load),
                                         ('Prompt processing', prefill), ('Generation', generation)]]
        error = ('CUDA out of memory while allocating KV cache. Requested 2.4 GiB; 1.1 GiB available.'
                 if state == 'Failed' else 'Compute deadline exceeded after 30 seconds.' if state == 'Timeout'
                 else 'User quota reached before compute admission.' if state == 'Refused' else '')
        legacy = state == 'No ending' or i == 22
        active = state in ('Running', 'Queued')
        trace = hashlib.sha256(f'trace-{i // 3}'.encode()).hexdigest()[:32]
        ingress = f'owg-{hashlib.sha256(str(i).encode()).hexdigest()[:16]}'
        at = now - timedelta(seconds=i * 43 + 12)
        records.append(dict(id=48291-i, at=at.isoformat(), state=state, model=model, service=service,
                            level=['normal','sensitive','restricted'][i % 3],
                            node=node, user=['jordan', 'alex', 'research-bot', 'pat'][i % 4],
                            group=['admins', 'design', 'research', 'design'][i % 4],
                            caller=['Text chat', 'Hermes', 'Batch API', 'Studio'][i % 4],
                            application=['web', 'hermes-cli', 'nightly-index', 'web'][i % 4],
                            role='coding' if i % 5 == 1 else 'assistant', path=path,
                            total=None if legacy or active else total, elapsed=total,
                            waited=None if legacy else wait, load=load,
                            ttft=None if legacy or state != 'Done' or service != 'LoadCoach' or path.endswith('embeddings') else round(wait+load+prefill, 3),
                            input=1124 + i * 37 if not legacy and service == 'LoadCoach' else None,
                            output=(0 if state in ('Failed', 'Refused', 'Queued') else 156 + i * 3)
                            if not legacy and service == 'LoadCoach' and not path.endswith('embeddings') else None,
                            rate=round((156+i*3)/generation, 1) if generation and state == 'Done' and path.endswith('completions') else None,
                            cost=round(.00018 * (i % 7 + 1), 5) if state == 'Done' else None,
                            status=500 if state == 'Failed' else 504 if state == 'Timeout' else 429 if state == 'Refused' else 200 if state == 'Done' else None,
                            error=error, error_code='cuda_oom' if state == 'Failed' else 'deadline_exceeded' if state == 'Timeout' else 'quota_exceeded' if state == 'Refused' else '',
                            phases=[] if legacy else phases, legacy=legacy, active=active,
                            request_id=ingress, trace_id=trace,
                            fingerprint=hashlib.sha256(model.encode()).hexdigest(), build='llama.cpp · sample b7124',
                            retained=i < 32, context=32768, cards=['GPU 0'], hosted=False,
                            prompt=('A quiet mountain observatory at dusk, architectural photography.' if service == 'MirrorWall'
                                    else 'Read aloud: The deployment checks have passed.' if service == 'SoundRoom'
                                    else 'Embed: Verify the service process and effective source.' if path.endswith('embeddings')
                                    else 'Summarize the deployment checks and explain which evidence confirms the running source.'),
                            answer=('Image generated · 1024 × 1024 · artifact omitted from this metadata preview.' if service == 'MirrorWall'
                                    else 'Speech generated · WAV · artifact omitted from this metadata preview.' if service == 'SoundRoom'
                                    else 'Embedding generated · 768 dimensions · vector omitted from this preview.' if path.endswith('embeddings')
                                    else 'Verify the service process working directory, inspect its imported module path, then exercise the updated endpoint. Record the effective commit and the observed behavior.') if state == 'Done' else '',
                            events=[dict(at=at.isoformat(), kind='request', what='Request admitted', detail=f'{service} / {node}'),
                                    dict(at=(at+timedelta(seconds=wait)).isoformat(), kind='model', what='Model selected', detail=model)],
                            spans=[dict(operation='Ingress', service='Turnstile', machine='front', seconds=None if active else round(total+.04,3), parent='—', id=f'{i:04x}01', depth=0, outcome='Error' if error else state),
                                   dict(operation='Authentication', service='Turnstile', machine='front', seconds=.012, parent=f'{i:04x}01', id=f'{i:04x}02', depth=1, outcome='Done'),
                                   dict(operation='Dispatch', service='WeightRoom', machine='front', seconds=.018, parent=f'{i:04x}01', id=f'{i:04x}03', depth=1, outcome='Done'),
                                   dict(operation='Compute request', service=service, machine=node, seconds=None if legacy or active else total, parent=f'{i:04x}03', id=f'{i:04x}04', depth=2, outcome=state)],
                            feedback=0))
    return records


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['timestamp'] = str
    static = ROOT / 'weightroom/static'
    return pages.get_template('request_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'requests.css', 'request-designs.css')),
        javascript=(static / 'request-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), records=samples(),
        nodes=['atlas', 'orion', 'lyra'], groups=['admins', 'design', 'research'],
        user_groups={'jordan': ['admins'], 'alex': ['design'], 'research-bot': ['research'], 'pat': ['design']})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/request-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
