"""Navigation stays usable while independently authorized page data is pending."""

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from engines import engine_for
from loadcoach.log import Log
from loadcoach.usage import report
from weightroom.app import Settings, build_app


@pytest.fixture
def deferred_console(tmp_path):
    """Use deterministic delayed data sources without network, inference or worker discovery."""
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    usage = {**report(Log(':memory:').db, {}), 'everyone': True, 'keep_days': 0}
    state = {'delay': False, 'authorized': True, 'fail': False}

    async def upstream(request):
        """Keep authentication fast and delay only the actual report or file read."""
        path = request.url.path
        if path == '/loadcoach/me':
            return httpx.Response(200, json={'name': 'owner', 'identity': 'owner',
                'can': ['manage-models', 'see-log'], 'models': [], 'choices': {'show_telemetry': False}}) if state['authorized'] else httpx.Response(401)
        if path in ('/turnstile/models', '/loadcoach/usage', '/loadcoach/requests', '/loadcoach/events', '/turnstile/models/files'):
            calls.append(path)
            if state['delay']:
                entered.set()
                await release.wait()
        if path == '/loadcoach/usage' and state['fail']:
            return httpx.Response(503, json={'error': {'message': 'Usage is temporarily unavailable.'}})
        values = {
            '/turnstile/models': {'models': [], 'to_measure': [], 'measuring': None},
            '/turnstile/machines': {'machines': [{'name': 'compute', 'state': 'ready', 'status_live': True}]},
            '/turnstile/machines/compute/engines': {'engines': [{'id': 'llama_cpp', 'label': 'llama.cpp', 'enabled': True, 'schema': engine_for().schema()}]},
            '/turnstile/models/files': {'nodes': []},
            '/loadcoach/usage': usage,
            '/loadcoach/requests': {'requests': [], 'total': 0, 'page': 1, 'choices': {}, 'accesses': []},
            '/loadcoach/events': {'events': []},
            '/loadcoach/event-choices': {'choices': {key: [] for key in ('kind', 'what', 'model', 'trace', 'user')}, 'user_groups': {}, 'groups': []},
        }
        return httpx.Response(200, json=values[path]) if path in values else httpx.Response(503, json={'error': {'message': 'Unavailable'}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://front:8442')
    app = build_app(Settings(compute='http://compute', key='service', data=str(tmp_path)), client)
    with TestClient(app) as browser:
        browser.cookies.set('weightroom', 'owner-key')
        yield browser, calls, state, entered, release


def test_navigation_does_not_wait_for_reports_or_model_files(deferred_console):
    browser, calls, _, _, _ = deferred_console
    for path, region in [('/usage?model=chosen', 'usage-report'), ('/models', 'models'),
                         ('/requests', 'requests-report'), ('/log', 'log-view')]:
        response = browser.get(path)
        assert response.status_code == 200
        assert f'id="{region}" data-deferred-loader' in response.text
        assert 'aria-busy="true"' in response.text and 'Retry loading' in response.text
        assert response.headers['cache-control'] == 'no-store'
    assert not calls
    assert '/usage?model=chosen&amp;part=report' in browser.get('/usage?model=chosen').text
    response = browser.get('/models?part=list')
    assert response.status_code == 200 and 'id="models-table"' in response.text and '<html' not in response.text
    assert '/turnstile/models/files' not in calls
    response = browser.get('/models?part=editor')
    assert response.status_code == 200 and 'data-create-model' in response.text
    assert calls.count('/turnstile/models/files') == 1


def test_slow_report_does_not_block_navigation_and_expired_sessions_redirect(deferred_console):
    browser, _, state, entered, release = deferred_console
    state['delay'] = True

    async def during_report():
        """Navigate on the same event loop while the report's upstream read is suspended."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser.app), base_url='http://testserver',
                                     cookies={'weightroom': 'owner-key'}) as client:
            pending = asyncio.create_task(client.get('/usage?part=report'))
            await asyncio.wait_for(entered.wait(), 1)
            for path in ('/models', '/usage', '/requests', '/log'):
                response = await asyncio.wait_for(client.get(path), .5)
                assert response.status_code == 200 and 'data-deferred-loader' in response.text
            release.set()
            response = await pending
            assert response.status_code == 200 and 'id="usage-data"' in response.text
            assert '<html' not in response.text

    browser.portal.call(during_report)
    state['authorized'] = False
    response = browser.get('/usage?part=report', headers={'HX-Request': 'true'}, follow_redirects=False)
    assert response.status_code == 204 and response.headers['HX-Redirect'].startswith('/sign-in')
