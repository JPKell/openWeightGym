"""Conversion ownership, inert sources, durable receipts and strict reference parity."""

import asyncio
import json
from pathlib import Path

import pytest

from loadcoach.classifier_jobs import ClassifierJobs
from loadcoach.classifier_parity import cases, evaluate


@pytest.fixture
def anyio_backend():
    return "asyncio"


def responses():
    """Make valid responses to the fixed thirty-item protocol."""
    rows = []
    for request in cases():
        kind = request['questions']['check']['type']
        answer = {'type': kind}
        if kind == 'noul':
            answer['noul'] = .8
        else:
            labels = ['billing', 'delivery', 'technical', 'other'] if kind == 'choice' else ['0', '1', '2']
            answer['probabilities'] = {label: .8 if i == 0 else .2 / (len(labels) - 1) for i, label in enumerate(labels)}
        rows.append({'answers': {'check': answer}})
    return rows


def test_parity_requires_every_item_and_both_thresholds():
    """Missing questions or even one changed winner cannot pass a 97% gate."""
    original = responses()
    assert evaluate(original, original)['verified']
    with pytest.raises(ValueError, match='30'):
        evaluate(original[:-1], original)
    changed = json.loads(json.dumps(original))
    changed[0]['answers']['check']['probabilities'].update(billing=.1, delivery=.7666666666666667)
    assert not evaluate(original, changed)['verified']
    changed = json.loads(json.dumps(original))
    for row in changed:
        if row['answers']['check']['type'] == 'noul':
            row['answers']['check']['noul'] = .6
    result = evaluate(original, changed)
    assert result['argmax_agreement'] == 1 and not result['verified']


@pytest.mark.anyio
async def test_conversion_refuses_code_and_paths_outside_classifier(tmp_path):
    """An installed tool receipt never grants repository-code execution or arbitrary paths."""
    root = tmp_path / 'classifier'
    source = root / 'source'
    source.mkdir(parents=True)
    jobs = ClassifierJobs(tmp_path / 'tools', '/bin/true', lambda: {'classifier': str(root)})
    (jobs.root / 'venv/bin').mkdir(parents=True)
    (jobs.root / 'venv/bin/python').touch()
    (jobs.root / 'installed.json').write_text(json.dumps({'commit':'abc', 'server_signature':jobs.server_signature()}))
    with pytest.raises(ValueError, match='inside'):
        await jobs.convert('alice', str(tmp_path))
    (source / 'config.json').write_text('{"auto_map":{"AutoModel":"repository.Custom"}}')
    with pytest.raises(ValueError, match='remote code'):
        await jobs.convert('alice', str(source))
    assert not list(jobs.jobs.root.iterdir())


@pytest.mark.anyio
async def test_conversion_manifest_and_receipts_preserve_unverified_state(tmp_path, monkeypatch):
    """Actual worker outcomes, rather than subprocess success, decide verification."""
    root = tmp_path / 'classifier'
    source = root / 'source'
    source.mkdir(parents=True)
    (source / 'config.json').write_text('{}')
    changed = []
    jobs = ClassifierJobs(tmp_path / 'tools', '/bin/true', lambda: {'classifier': str(root)}, lambda *paths: changed.extend(paths))
    (jobs.root / 'venv/bin').mkdir(parents=True)
    (jobs.root / 'venv/bin/python').touch()
    (jobs.root / 'installed.json').write_text(json.dumps({'commit':'abc', 'server_signature':jobs.server_signature()}))

    async def command(identifier, *args, **kwargs):
        output = Path(args[args.index('--output') + 1])
        output.mkdir()
        (output / 'manifest.json').write_text(json.dumps({'state': 'unverified', 'parity': {'verified': False, 'reason': 'Reference unavailable'}}))
        (output / 'model.gguf').write_bytes(b'original tensor bytes')
    monkeypatch.setattr(jobs, 'command', command)
    receipt = await jobs.convert('alice', str(source / 'config.json'))
    await asyncio.gather(*jobs.jobs.tasks)
    result = jobs.jobs.get(receipt['id'], 'alice')
    assert result['state'] == 'unverified' and not result['parity']['verified']
    assert (source / 'config.json').read_text() == '{}'
    assert len(changed) == 2
    with pytest.raises(PermissionError):
        jobs.jobs.get(receipt['id'], 'bob')
    with pytest.raises(ValueError, match='already'):
        await jobs.convert('alice', str(source))


@pytest.mark.anyio
async def test_shutdown_cancels_worker_and_restart_keeps_receipt(tmp_path, monkeypatch):
    """Restart never silently resumes or reports a cancelled conversion as verified."""
    jobs = ClassifierJobs(tmp_path / 'tools', '/bin/true', lambda: {})
    started = asyncio.Event()

    async def worker(identifier):
        started.set()
        await asyncio.Event().wait()
    receipt = jobs.jobs.start(jobs.jobs.create('alice', 'classifier-conversion', 'source'), worker)
    await started.wait()
    await jobs.close()
    recovered = ClassifierJobs(tmp_path / 'tools', '/bin/true', lambda: {})
    assert recovered.jobs.get(receipt['id'], 'alice')['state'] == 'interrupted'


@pytest.mark.anyio
async def test_tool_receipt_is_written_last_and_expires_when_runtime_changes(tmp_path, monkeypatch):
    """The converter follows the installed binary rather than silently keeping stale source."""
    import io
    server = tmp_path / 'llama-server'
    server.write_text('#!/bin/sh\necho "version build 12008, commit abcdefa"\n')
    server.chmod(0o755)
    jobs = ClassifierJobs(tmp_path / 'tools', str(server), lambda: {})
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: io.BytesIO(json.dumps({'sha': 'abcdefa' + '0' * 33}).encode()))
    commands = []
    async def command(identifier, *args, **kwargs):
        commands.append([str(arg) for arg in args])
        if '-m' in args and 'venv' in args:
            python = jobs.root / 'venv/bin/python'
            python.parent.mkdir(parents=True)
            python.touch()
    monkeypatch.setattr(jobs, 'command', command)
    receipt = await jobs.install('alice')
    await asyncio.gather(*jobs.jobs.tasks)
    assert jobs.jobs.get(receipt['id'], 'alice')['state'] == 'complete'
    assert jobs.status()['available']
    assert any('abcdefa' + '0' * 33 in args for args in commands)
    server.write_text(server.read_text() + '# changed runtime\n')
    assert not jobs.status()['available']
