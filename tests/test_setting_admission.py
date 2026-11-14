"""Live queue, residency, routing and durable batch settings use catalogue contracts."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.roles import seconds_to_answer
from loadcoach.runner import Runner,Server,Ticket,Refusal
from loadcoach.setting_catalog import get
from test_app import model,fake_llama_server  # noqa: F401
from test_setting_requests import control  # noqa: F401
from test_weightroom import desk,sign_in  # noqa: F401


@pytest.fixture
def runner(monkeypatch):
    models = Models(':memory:')
    for name in ('old','recent','new'):
        models.put(name,model(name+'.gguf',0))
    made = Runner(Config(),None,models=models)
    monkeypatch.setattr(made,'host_memory',lambda **_:dict(free=100000,service_available=100000,service_free=100000,service_used=0,service_limit=None))
    yield made
    models.db.close()


def test_queue_cap_counts_waiting_inference_and_preserves_admitted_work(runner):
    async def exercise():
        runner.config.max_queue = 1
        old = Ticket('old','owner','here','normal')
        old.turn.set()
        await runner.acquire(old)
        second = Ticket('new','owner','here','normal')
        with pytest.raises(Refusal) as problem:
            await runner.acquire(second)
        assert problem.value.status == 503 and problem.value.retry_after > 0
        assert runner.waiting == [old]
        runner.config.max_queue = 2
        second.turn.set()
        assert await runner.acquire(second) is second
        runner.config.max_queue = 1  # lower live cap never removes an already admitted ticket
        assert runner.waiting == [old,second]
        runner.config.max_queue = 0
        third = Ticket('recent','owner','here','normal')
        third.turn.set()
        await runner.acquire(third)
        assert len(runner.waiting) == 3
    asyncio.run(exercise())


def residents(runner):
    old = Server('old',(),runner.models.get('old'),ready=True,rss_mib=256,idle_since=time.monotonic()-60)
    recent = Server('recent',(),runner.models.get('recent'),ready=True,rss_mib=256,idle_since=time.monotonic()-10)
    runner.servers = {'old':old,'recent':recent}
    return old,recent


def test_resident_cap_evicts_oldest_eligible_idle_model_only_on_new_placement(runner):
    old,recent = residents(runner)
    assert runner.fit('new',runner.models.get('new'),set())[2] == []
    runner.config.max_loaded_models = 2
    assert runner.fit('new',runner.models.get('new'),set())[2] == [old]
    assert runner.servers == {'old':old,'recent':recent}
    runner.config.max_loaded_models = 1
    assert runner.fit('new',runner.models.get('new'),set())[2] == [old,recent]
    runner.config.max_loaded_models = 0
    assert runner.fit('new',runner.models.get('new'),set())[2] == []


@pytest.mark.parametrize('protection',['running','held','locked','loading','idle interval','retiring'])
def test_resident_cap_protects_active_and_reserved_models(runner,protection):
    old,recent = residents(runner)
    runner.config.max_loaded_models = 1
    if protection == 'running':
        runner.running.append(Ticket('old','owner','here','normal',server=old))
    elif protection == 'held':
        runner.holds['old','owner'] = time.monotonic()+60
    elif protection == 'locked':
        runner.models.set_locked('old',True)
    elif protection == 'loading':
        old.ready = False
    elif protection == 'idle interval':
        runner.config.model_idle_minutes = 10
    else:
        del runner.servers['old']
        runner.retiring.add(old)
    assert runner.fit('new',runner.models.get('new'),set()) is None


def test_node_caps_change_the_actual_runner_without_restarting(tmp_path,monkeypatch):
    import loadcoach.app as service
    captured = []
    def make(*args,**kwargs):
        made = Runner(*args,**kwargs)
        captured.append(made)
        return made
    monkeypatch.setattr(service,'Runner',make)
    config = Config(database=tmp_path/'node.db',nvidia_smi='no-such-program')
    app = service.build_app(config)
    with TestClient(app) as api:
        for value in (3,1,0):
            answer = api.post('/loadcoach/settings/loadcoach',json={'max_queue':value,'max_loaded_models':value})
            assert answer.status_code == 200,answer.text
            assert captured[0].config.max_queue == value and captured[0].config.max_loaded_models == value
        assert len(captured) == 1
        assert api.post('/loadcoach/settings/loadcoach',json={'max_queue':-1}).status_code == 400


def facts():
    return {'models':{name:dict(why_not=None,hosted=False,sees=False,tools=False,room=0,placed=False,context=0,
                               warm=name=='loaded',resident=name=='loaded',speed=None,load=None)
                      for name in ('best','loaded')},'environment':{}}


def test_role_defaults_and_unknown_timing_preferences_apply_without_restart(control):
    api,app,_,_ = control
    routing = app.state.routing
    routing.roles.put('code',{'models':{'best':100,'loaded':70}})
    def choose(strength=None):
        return routing.chosen(('code',strength),{},facts())
    assert choose()[1][0] == 'best'
    assert api.post('/loadcoach/settings/turnstile',json={'role_loaded_bonus':50,'role_fast_loaded_bonus':200,
                                                       'role_unknown_speed':20,'role_unknown_load':120}).status_code == 200
    seen,chosen = choose()
    assert chosen[0] == 'loaded'
    assert next(one for one in seen if one['name']=='loaded')['fast_loaded_factor'] == 3
    cold = next(one for one in seen if one['name']=='best')
    assert seconds_to_answer(cold) == 140  # 120 s load + 400 / 20 provisional tokens/s
    # Explicit per-role choices retain precedence over the global defaults.
    routing.roles.put('code',{'models':{'best':100,'loaded':70},'loaded_bonus':0,'fast_loaded_bonus':0})
    seen,chosen = choose()
    assert chosen[0] == 'best' and all(one['fast_loaded_factor'] == 1 for one in seen)
    assert api.post('/loadcoach/settings/turnstile',json={'role_loaded_bonus':None,'role_fast_loaded_bonus':None,
                                                       'role_unknown_speed':None,'role_unknown_load':None}).status_code == 200
    cold = next(one for one in choose()[0] if one['name']=='best')
    assert seconds_to_answer(cold) == 100


def test_role_attempt_limit_applies_to_the_next_request(control,monkeypatch):
    import httpx
    api,app,_,_ = control
    routing = app.state.routing
    routing.roles.put('code',{'models':{'best':100,'loaded':70}})
    selected = facts()
    selected['placements'] = {'best':['compute'],'loaded':['compute']}
    async def current(*args):
        return selected
    routing.facts_for = current
    routing.hosted = None
    attempts = []
    async def send(*args,**kwargs):
        attempts.append(args[4])
        return httpx.Response(502,content=b'not loaded',request=httpx.Request('POST','http://compute/v1/chat/completions'))
    routing.send_placed = send
    routing.relay = lambda upstream,*args: __import__('starlette.responses',fromlist=['Response']).Response(upstream.content,status_code=upstream.status_code)
    # The real role API through TestClient exercises the live attempt loop.
    for maximum in (1,2):
        assert api.post('/loadcoach/settings/turnstile',json={'role_tries':maximum}).status_code == 200
        attempts.clear()
        answer = api.post('/v1/chat/completions',json={'model':'code','messages':[]})
        assert answer.status_code == 502,answer.text
        assert len(attempts) == maximum


def batch_lines(count):
    return b'\n'.join(json.dumps({'custom_id':str(n),'url':'/v1/chat/completions','body':{'model':'best'}}).encode() for n in range(count))


def test_batch_limits_and_interrupted_attempts_read_current_settings(control):
    api,app,_,_ = control
    worker = app.state.batches
    assert api.post('/loadcoach/settings/turnstile',json={'batch_max_lines':1,'batch_max_mib':1,'batch_tries':1}).status_code == 200
    assert api.post('/v1/files',data={'purpose':'batch'},files={'file':('huge',b'x'*(2**20))}).status_code == 413
    uploaded = api.post('/v1/files',data={'purpose':'batch'},files={'file':('small',batch_lines(2))})
    assert uploaded.status_code == 200
    body = {'input_file_id':uploaded.json()['id'],'endpoint':'/v1/chat/completions','completion_window':'24h'}
    assert api.post('/v1/batches',json=body).status_code == 400
    kept = worker.store
    file = kept.put_file('owner','one','batch',batch_lines(1))
    from loadcoach.batches import lines_of
    batch = kept.start('owner','/v1/chat/completions',file['id'],lines_of(batch_lines(1),'/v1/chat/completions'))
    assert kept.next_line(batch['id'])
    assert kept.next_line(batch['id']) is None
    assert kept.get(batch['id'])['request_counts']['failed'] == 1
    assert api.post('/loadcoach/settings/turnstile',json={'batch_max_mib':2,'batch_max_lines':2,'batch_tries':2}).status_code == 200
    assert api.post('/v1/files',data={'purpose':'batch'},files={'file':('larger',b'x'*(2**20))}).status_code == 200
    # Validate at the real start boundary without running inference on this pool-timeout fixture.
    from unittest.mock import patch
    with patch.object(worker,'work'):
        assert api.post('/v1/batches',json=body).status_code == 200
    next_batch = kept.start('owner','/v1/chat/completions',file['id'],lines_of(batch_lines(1),'/v1/chat/completions'))
    assert kept.next_line(next_batch['id']) and kept.next_line(next_batch['id'])
    assert kept.next_line(next_batch['id']) is None


def test_pending_batch_retry_delay_changes_without_restart(control,monkeypatch):
    import turnstile.batches as module
    api,app,_,_ = control
    worker = app.state.batches
    clock,sleeps = [0.0],[]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        app.state.console_settings.changes.put('turnstile','batch_retry_seconds',2,'test')
    monkeypatch.setattr(module,'asyncio',SimpleNamespace(sleep=sleep))
    assert api.post('/loadcoach/settings/turnstile',json={'batch_retry_seconds':30}).status_code == 200
    asyncio.run(worker.retry_delay())
    assert sum(sleeps) == 2


@pytest.mark.parametrize('program,name,value',[
    ('loadcoach','max_queue',-1),('loadcoach','max_queue',100001),('loadcoach','max_loaded_models',1001),
    ('turnstile','role_tries',0),('turnstile','role_tries',21),('turnstile','role_loaded_bonus',101),
    ('turnstile','role_fast_loaded_bonus',501),('turnstile','role_unknown_speed',0),('turnstile','role_unknown_load',-1),
    ('turnstile','batch_max_lines',0),('turnstile','batch_max_mib',1025),('turnstile','batch_tries',0),
    ('turnstile','batch_retry_seconds',0),('turnstile','batch_retry_seconds',3601),
])
def test_ranges_are_validated_by_catalogue(program,name,value):
    with pytest.raises(ValueError):
        get(program,name).check(value)


def test_advanced_request_controls_are_collapsed_and_have_native_save_fallbacks(desk):
    from loadcoach.settings import said
    from turnstile.app import Settings
    api,switch,_ = desk
    switch.front.state.console_settings.changes.seen('turnstile',said(vars(Settings()),{}, {},'turnstile'))
    sign_in(api,'jordan')
    page = api.get('/settings/requests')
    assert page.status_code == 200
    position = page.text.index('name="value-role_tries"')
    began = page.text.rfind('<details',0,position)
    assert began >= 0 and ' open' not in page.text[began:page.text.index('>',began)]
    assert '>Save turnstile</button>' in page.text[position:]


@pytest.mark.parametrize('width',[1440,390])
def test_advanced_controls_search_errors_and_poll_replacement_fit_the_workspace(desk,width):
    from test_console_browser import browser_context,playwright
    from loadcoach.settings import said
    from turnstile.app import Settings
    server,switch,_ = desk
    switch.front.state.console_settings.changes.seen('turnstile',said(vars(Settings()),{}, {},'turnstile'))
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/settings/requests')
        search = page.get_by_label('Find a setting',exact=True)
        search.fill('batch retry')
        field = page.locator('[name="value-batch_retry_seconds"]')
        playwright.expect(field).to_be_visible()
        page.evaluate("document.querySelector('[name=value-batch_retry_seconds]').closest('.settings-row').replaceWith(document.querySelector('[name=value-batch_retry_seconds]').closest('.settings-row').cloneNode(true))")
        playwright.expect(field).to_be_visible()
        search.fill('no matching field at all')
        playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
        page.get_by_role('button',name='Clear filters',exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


def test_advanced_controls_keep_native_forms_without_javascript(desk):
    from test_console_browser import playwright
    from front_helpers import serve_browser_request
    from loadcoach.settings import said
    from turnstile.app import Settings
    server,switch,_ = desk
    switch.front.state.console_settings.changes.seen('turnstile',said(vars(Settings()),{}, {},'turnstile'))
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(java_script_enabled=False,viewport={'width':390,'height':1000})
        page.route('http://console.test/**',lambda route:serve_browser_request(server,'http://console.test',route))
        page.goto('http://console.test/settings/requests')
        page.locator('summary').filter(has_text='Advanced').click()
        field = page.locator('[name="value-role_tries"]')
        playwright.expect(field).to_be_visible()
        field.fill('4')
        page.locator('form').filter(has=field).get_by_role('button',name='Save turnstile',exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Saved.')
        assert switch.front.state.console_settings.changes.of('turnstile')['role_tries'] == 4
        browser.close()


def test_live_queue_overload_is_a_real_503_with_retry_after(fake_llama_server,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from loadcoach.app import build_app
    path = tmp_path/'model.gguf'
    path.touch()
    config = Config(models={'one':model(str(path),0)},llama_server=fake_llama_server,nvidia_smi='no-such-program',max_wait=10)
    with TestClient(build_app(config)) as api,ThreadPoolExecutor(max_workers=2) as pool:
        def request(delay=0):
            return api.post('/v1/chat/completions',json={'model':'one','messages':[],'sleep':delay})
        assert request().status_code == 200  # load once before the queue exercise
        assert api.post('/loadcoach/settings/loadcoach',json={'max_queue':1}).status_code == 200
        first = pool.submit(request,2)
        deadline = time.monotonic()+3
        while not api.get('/loadcoach/status').json()['running'] and time.monotonic() < deadline:
            time.sleep(.01)
        second = pool.submit(request)
        deadline = time.monotonic()+3
        while not api.get('/loadcoach/status').json()['waiting'] and time.monotonic() < deadline:
            time.sleep(.01)
        assert api.get('/loadcoach/status').json()['waiting']
        refused = request()
        assert refused.status_code == 503 and int(refused.headers['retry-after']) > 0
        assert 'queue is full' in refused.text
        assert api.post('/loadcoach/settings/loadcoach',json={'max_queue':0}).status_code == 200
        assert request().status_code == 200
        assert first.result().status_code == second.result().status_code == 200


def test_live_resident_cap_reclaims_idle_models_during_real_placement(fake_llama_server,tmp_path):
    from loadcoach.app import build_app
    from test_app import alive
    models = {}
    for name in ('old','recent','new'):
        path = tmp_path/(name+'.gguf')
        path.touch()
        models[name] = model(str(path),0)
    config = Config(models=models,llama_server=fake_llama_server,nvidia_smi='no-such-program',max_wait=10)
    with TestClient(build_app(config)) as api:
        def request(name):
            answer = api.post('/v1/chat/completions',json={'model':name,'messages':[]})
            assert answer.status_code == 200,answer.text
            return answer.json()['pid']
        old,recent = request('old'),request('recent')
        assert len(api.get('/loadcoach/status').json()['loaded']) == 2
        assert api.post('/loadcoach/settings/loadcoach',json={'max_loaded_models':1}).status_code == 200
        assert alive(old) and alive(recent)  # cap changes never stop existing work on save
        new = request('new')
        assert len(api.get('/loadcoach/status').json()['loaded']) == 1
        assert alive(new) and not alive(old) and not alive(recent)
        assert api.post('/loadcoach/settings/loadcoach',json={'max_loaded_models':0}).status_code == 200
        request('old')
        assert len(api.get('/loadcoach/status').json()['loaded']) == 2
