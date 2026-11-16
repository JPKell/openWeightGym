"""Developer MCP tools stay current without executing source or trusting stale vectors."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from developer_tools.code_index import CodeIndex, Embedder, normalized
from toollocker.tools import code, load


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    (root / 'sample.py').write_text('''"""Account operations."""
import pathlib


def save():
    """Persist a customer record in the database."""
    return 42


def run():
    """Run the operation."""
    return save()


class Base:
    """Base account."""


class Account(Base):
    """Customer account."""
    def close(self):
        """Close the account."""
        return save()
''')
    git(root, 'add', 'sample.py')
    return root


@pytest.fixture
def index(repo, tmp_path):
    value = CodeIndex(str(repo), {'index_directory': str(tmp_path / 'cache'), 'index_interval': 1})
    value.refresh()
    yield value
    value.close()


def symbol(index, name):
    return next(s for s in index.symbols('', 50, 0)['symbols'] if s['qualname'] == name)


def test_graph_source_search_and_pagination(index):
    save = symbol(index, 'save')
    assert index.symbol(save['id'], 0, 20)['source'].startswith('def save():')
    assert index.symbol(save['id'], 1, 1)['next_offset'] == 2
    assert index.search('customer database', False, 10)['hits'][0]['id'] == save['id']
    assert index.symbols('', 2, 0)['next_offset'] == 2
    edges = index.related(save['id'], 'incoming', 20, 0)['relationships']
    assert len([r for r in edges if r['kind'] == 'calls']) == 2
    assert all(r['certainty'] == 'syntactic_candidate' for r in edges if r['kind'] == 'calls')
    account = symbol(index, 'Account')
    assert any(e['target'] == 'Base' for e in index.related(account['id'], 'outgoing', 20, 0)['relationships'])
    assert index.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='Semantic search needs'):
        index.search('save customer', True, 10)


def test_incremental_change_rename_delete_and_parse_failure(index, repo):
    before = symbol(index, 'save')
    path = repo / 'sample.py'
    path.write_text('def updated():\n    """Replacement operation."""\n    pass\n')
    index.refresh()
    assert not index.symbols('save', 20, 0)['symbols']
    assert symbol(index, 'updated')['hash'] != before['hash']
    path.write_text('def broken(:\n')
    index.refresh()
    assert not index.symbols('', 20, 0)['symbols']
    assert index.status()['excluded'][0]['error'] == 'SyntaxError'
    path.write_text('def restored():\n    pass\n')
    git(repo, 'mv', 'sample.py', 'new.py')
    index.refresh()
    assert symbol(index, 'restored')['path'] == 'new.py'
    (repo / 'new.py').unlink()
    index.refresh()
    assert index.status()['symbols'] == 0
    git(repo, 'rm', '--cached', 'new.py')
    index.refresh()
    assert index.status()['files'] == 0


def test_untracked_and_symlink_sources_are_excluded(index, repo, tmp_path):
    private = tmp_path / 'secret.py'
    private.write_text('def sensitive():\n    pass\n')
    (repo / 'untracked.py').write_text(private.read_text())
    (repo / 'linked.py').symlink_to(private)
    git(repo, 'add', 'linked.py')
    index.refresh()
    assert not index.symbols('sensitive', 20, 0)['symbols']
    assert index.status()['excluded'][0]['path'] == 'linked.py'
    # A tracked parent replaced by a symlink is also refused.
    folder = repo / 'folder'
    folder.mkdir()
    (folder / 'nested.py').write_text('def original():\n    pass\n')
    git(repo, 'add', 'folder/nested.py')
    (folder / 'nested.py').unlink()
    folder.rmdir()
    folder.symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / 'nested.py').write_text(private.read_text())
    index.refresh()
    assert not index.symbols('sensitive', 20, 0)['symbols']
    for path in ('../secret.py', '/etc/passwd', 'folder/nested.py'):
        with pytest.raises((ValueError, OSError)):
            index.read(path)


def test_background_refresh_without_a_query(index, repo):
    index.start()
    (repo / 'sample.py').write_text('def autonomous():\n    pass\n')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if index.symbols('autonomous', 20, 0)['symbols']:
            break
        time.sleep(.05)
    else:
        pytest.fail('background cycle did not discover the edit')
    assert not index.symbols('save', 20, 0)['symbols']


def test_embeddings_pending_retry_source_race_and_identity(index, repo, monkeypatch, tmp_path):
    index.embedder.url = 'http://localhost/v1/embeddings'
    def unavailable(texts):
        raise ValueError('offline')
    monkeypatch.setattr(index.embedder, 'embed', unavailable)
    index.embed_pending()
    assert index.status()['embedding_error'] == 'offline'
    assert index.search('database', False, 5)['hits']

    def embed(texts):
        return [normalized([1, 0] if 'database' in text or 'persist' in text else [0, 1]) for text in texts]
    monkeypatch.setattr(index.embedder, 'embed', embed)
    index.embed_pending()
    assert index.status()['pending_docstrings'] == 0
    assert not index.status()['embedding_error']
    assert index.search('persist', True, 3)['hits'][0]['qualname'] == 'save'
    # Never publish a response for a document changed while the provider was working.
    (repo / 'sample.py').write_text('def newer():\n    """New source."""\n    pass\n')
    index.refresh()
    def racing(texts):
        (repo / 'sample.py').write_text('def newest():\n    """Newest source."""\n    pass\n')
        index.refresh()
        return [[1., 0.] for _ in texts]
    monkeypatch.setattr(index.embedder, 'embed', racing)
    index.embed_pending()
    assert index.status()['vectors'] == 0
    monkeypatch.setattr(index.embedder, 'embed', embed)
    index.embed_pending()
    assert index.status()['vectors'] == 1
    # A newly configured model/revision cannot reuse old vectors.
    replacement = CodeIndex(str(repo), {'index_directory': str(tmp_path / 'cache'), 'embedding_url': 'http://localhost/v1/embeddings', 'embedding_model': 'new', 'embedding_revision': 'weights-hash'})
    assert replacement.status()['vectors'] == 0
    replacement.close()


def test_embedding_validation_and_bounds(index):
    for values in ([], [0, 0], [float('nan')], [True], [float('inf')], [1] * 4097):
        with pytest.raises(ValueError):
            normalized(values)
    with pytest.raises(ValueError):
        Embedder({'embedding_url': 'https://secret:token@example.com/embeddings', 'embedding_model': 'm', 'embedding_revision': 'r'})
    for size in (0, -1, 100000, True):
        with pytest.raises(ValueError):
            index.symbols('', size, 0)
    with pytest.raises(ValueError):
        index.search('   ', False, 1)
    assert index.search('" OR *', False, 1)['mode'] == 'lexical'


def test_protocol_and_existing_entrypoint(repo, tmp_path):
    settings = tmp_path / 'tools.toml'
    settings.write_text(f'[code]\nrepositories = [{json.dumps(str(repo))}]\nindex_directory = {json.dumps(str(tmp_path / "cache"))}\n')
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'docstring_search', 'arguments': {'query': 'customer database'}}},
    ]
    result = subprocess.run([sys.executable, '-m', 'toollocker.tools.code', '--settings', str(settings)], input='\n'.join(map(json.dumps, messages)) + '\n', capture_output=True, text=True, timeout=30, check=True)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies[0]['result']['serverInfo']['name'] == 'code'
    assert {'symbols', 'related', 'docstring_search', 'index_status', 'search'} <= {t['name'] for t in replies[1]['result']['tools']}
    answer = replies[2]['result']
    assert not answer['isError']
    assert json.loads(answer['content'][0]['text'])['hits'][0]['qualname'] == 'save'


def test_compatibility_folder_exports(repo, tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'code.py').write_text(Path('toollocker/tools/code.py').read_text())
    try:
        found = load(tools, {'code': {'repositories': [str(repo)], 'index_directory': str(tmp_path / 'cache')}})
        assert {t.__name__ for t in found} == {t.__name__ for t in code.TOOLS}
        assert code.repositories() == str(repo)
    finally:
        for value in code.INDEXES.values():
            value.close()
        code.INDEXES.clear()
        code.REPOS.clear()


def test_failed_batch_does_not_starve_later_docstrings(index, repo, monkeypatch):
    (repo / 'sample.py').write_text('\n'.join(f'def item{i:02}():\n    """Document {i}."""\n    pass\n' for i in range(20)))
    index.refresh()
    index.embedder.url = 'http://localhost/v1/embeddings'
    attempts = []
    def embed(texts):
        attempts.append(texts)
        if len(attempts) == 1:
            raise ValueError('batch refused')
        return [[1., 0.] for _ in texts]
    monkeypatch.setattr(index.embedder, 'embed', embed)
    index.embed_pending()
    assert index.status()['failed_docstrings'] == 16
    index.embed_pending()
    assert attempts[1][0].startswith('item16\n')
    index.embed_pending()
    assert index.status()['pending_docstrings'] == 0
    assert index.status()['failed_docstrings'] == 0


def test_failed_refresh_rolls_back_and_raced_source_is_refused(index, repo, monkeypatch):
    from developer_tools import code_index
    original = index.status()['updated']
    row = symbol(index, 'save')
    (repo / 'sample.py').write_text('def replacement():\n    pass\n')
    with pytest.raises(ValueError, match='Source changed'):
        index.symbol(row['id'], 0, 20)
    monkeypatch.setattr(code_index, 'MAX_TOTAL', 1)
    with pytest.raises(RuntimeError):
        index.refresh()
    assert index.status()['updated'] == original
    assert symbol(index, 'save')['hash'] == row['hash']


def test_source_read_preserves_declared_python_encoding(index, repo):
    (repo / 'sample.py').write_bytes('# coding: latin-1\ndef café():\n    """Crème brûlée."""\n    pass\n'.encode('latin-1'))
    index.refresh()
    row = symbol(index, 'café')
    result = index.symbol(row['id'], 0, 20)
    assert 'Crème brûlée' in result['source'] and result['docstring'] == 'Crème brûlée.'
