"""A file page's adapter and projector helpers offer every node's models and make the new model where the base is (QA-103)."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from weightroom.app import Settings, build_app

ADAPTER = '/mnt/usb/ai/models/adapters/terse.gguf'
PROJECTOR = '/mnt/usb/ai/models/projectors/eyes.gguf'
BASE = {'flags': {'model': '/gpu/llm/base.gguf', 'ctx-size': 4096}, 'gpus': 1}


def stored(path, what, made_for):
    """One file in control-node storage, as the artifact inventory reports it."""
    return {'path': path, 'size': 1000, 'place': 'storage_' + what + 's', 'tier': 'storage', 'used_by': [], 'last_used': None,
            'facts': {'what': what, 'name': 'Terse' if what == 'adapter' else 'Base', 'made_for': made_for}, 'goes_with': []}


@pytest.fixture
def console(tmp_path):
    sent = []
    files = [stored(ADAPTER, 'adapter', 'Base'), stored(PROJECTOR, 'projector', 'Base')]
    held = {'gpu-1': []}

    async def upstream(request):
        path = request.url.path
        if path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'owner', 'identity': 'owner', 'can': ['manage-models'], 'models': [],
                                             'choices': {'show_telemetry': False}})
        if path == '/turnstile/artifacts/files':
            nodes = [{'name': 'front', 'answering': True, 'places': [], 'files': files},
                     {'name': 'gpu-1', 'answering': True, 'places': [], 'files': [{'path': one} for one in held['gpu-1']]}]
            return httpx.Response(200, json={'artifact_machine': 'front', 'places': [], 'files': files, 'front_enabled': True,
                                             'store_available': True, 'cleanup': {}, 'nodes': nodes, 'distributions': []})
        if path == '/turnstile/artifacts/file':
            return httpx.Response(200, json={'path': request.url.params['path'], 'size': 1000, 'facts': {}, 'metadata': {}})
        if path == '/turnstile/artifacts/models':  # what the page used to ask: the control node's store has no models
            return httpx.Response(200, json={'models': []})
        if path == '/turnstile/models':
            return httpx.Response(200, json={'models': [
                {'name': 'base', **BASE, 'machines': ['gpu-1'], 'locations': [{'machine': 'gpu-1', **BASE}]},
                {'name': 'embedder', 'embedding': True, 'flags': {'model': '/gpu/e.gguf'}, 'machines': ['gpu-1'],
                 'locations': [{'machine': 'gpu-1', 'embedding': True, 'flags': {'model': '/gpu/e.gguf'}}]}]})
        if request.method == 'POST' and path.startswith('/turnstile/'):
            sent.append((path, dict(request.url.params), json.loads(request.content)))
            if path == '/turnstile/models/install':
                return httpx.Response(202, json={'id': 'job-1'})
            return httpx.Response(201, json={'name': 'made'})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://front:8442')
    app = build_app(Settings(compute='http://compute', key='service', data=str(tmp_path)), client)
    with TestClient(app) as browser:
        browser.cookies.set('weightroom', 'owner-key')
        yield browser, sent, held


def test_a_stored_adapter_offers_the_compute_nodes_models_and_installs_onto_the_base_node(console):
    browser, sent, _ = console
    page = browser.get('/file', params={'path': ADAPTER})
    assert page.status_code == 200, page.text
    assert '<option value="base">' in page.text and 'embedder' not in page.text  # language models only
    assert 'value="adapt" disabled' not in page.text
    made = browser.post('/file', data={'path': ADAPTER, 'do': 'adapt', 'model': 'base', 'short': 'terse', 'name': 'base-terse'})
    assert sent == [('/turnstile/models/install', {}, {'name': 'base-terse', 'machine': 'gpu-1',
                     'settings': {**BASE, 'adapters': {'terse': {'file': ADAPTER}}}})]
    assert '/models' in str(made.url) and 'Making base-terse on gpu-1' in made.text


def test_a_stored_projector_makes_a_seeing_copy_and_leaves_the_base_as_it_was(console):
    browser, sent, _ = console
    assert '<option value="base">' in browser.get('/file', params={'path': PROJECTOR}).text
    browser.post('/file', data={'path': PROJECTOR, 'do': 'project', 'model': 'base', 'name': 'base-vision'})
    assert sent[0][2]['settings'] == {'flags': {**BASE['flags'], 'mmproj': PROJECTOR}, 'gpus': 1}
    assert BASE['flags'] == {'model': '/gpu/llm/base.gguf', 'ctx-size': 4096}


def test_a_file_already_on_the_node_is_made_at_once_without_copying(console):
    browser, sent, held = console
    held['gpu-1'].append(ADAPTER)
    made = browser.post('/file', data={'path': ADAPTER, 'do': 'adapt', 'model': 'base', 'short': 'terse', 'name': 'base-terse'})
    assert sent == [('/turnstile/machines/gpu-1/model-api/models/base-terse', {'new': '1'},
                     {**BASE, 'adapters': {'terse': {'file': ADAPTER}}})]
    assert str(made.url).split('?')[0].endswith('/models/base-terse')


@pytest.mark.parametrize('form, why', [
    ({'model': 'nope', 'short': 'terse', 'name': 'x'}, 'Choose the model this adapter is for.'),
    ({'model': 'base', 'short': 'terse', 'name': ''}, 'Give the new model a name.'),
    ({'model': 'base', 'short': '', 'name': 'x'}, 'Give the adapter a word'),
])
def test_incomplete_forms_are_refused_without_creating_anything(console, form, why):
    browser, sent, _ = console
    refused = browser.post('/file', data={'path': ADAPTER, 'do': 'adapt', **form})
    assert refused.status_code == 400 and why in refused.text and not sent
