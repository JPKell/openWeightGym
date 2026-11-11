"""A model's settings page as a node matrix: one column per node holding a copy, edited and saved together."""
from html.parser import HTMLParser

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import door, turnstile_to
from loadcoach.users import Users
from test_shared_model_settings import Fleet
from test_weightroom import GROUPS, sign_in
from weightroom.app import Settings, build_app, matrix_nodes, node_form


class Form(HTMLParser):
    """The fields a browser would send from the form marked `marker`: ticked boxes, chosen options, enabled
    fields; nothing inside <template> or <noscript>, nor a field that belongs to another form."""

    def __init__(self, page, marker):
        super().__init__()
        self.marker, self.inside, self.hidden, self.fields, self.select = marker, 0, 0, [], None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form' and self.marker in attrs:
            self.inside = 1
        elif tag in ('template', 'noscript'):
            self.hidden += 1
        if not self.inside or self.hidden or 'disabled' in attrs or attrs.get('form'):
            return
        if tag == 'input' and attrs.get('name') and attrs.get('type') not in ('submit', 'button'):
            if attrs.get('type') not in ('checkbox', 'radio') or 'checked' in attrs:
                self.fields.append([attrs['name'], attrs.get('value', 'on' if attrs.get('type') in ('checkbox', 'radio') else '')])
        elif tag == 'select' and attrs.get('name'):
            self.select = [attrs['name'], None, None]
        elif tag == 'option' and self.select:
            value = attrs.get('value', '')
            self.select[2] = value if self.select[2] is None else self.select[2]
            if 'selected' in attrs:
                self.select[1] = value

    def handle_endtag(self, tag):
        if tag in ('template', 'noscript'):
            self.hidden -= 1
        elif tag == 'select' and self.select:
            self.fields.append([self.select[0], self.select[1] if self.select[1] is not None else self.select[2]])
            self.select = None
        elif tag == 'form':
            self.inside = 0

    def set(self, name, value, nth=0):
        """Type `value` into the nth field called `name`."""
        [field for field in self.fields if field[0] == name][nth][1] = value
        return self

    def value_of(self, name, flag):
        """The value sent beside a flag row: flag rows are the `flag` and `value` fields in step."""
        prefix = name.removesuffix('flag')
        pairs = zip([v for k, v in self.fields if k == name], [i for i, (k, _) in enumerate(self.fields) if k == prefix + 'value'])
        return next(index for one, index in pairs if one == flag)

    def data(self, **more):
        sent = {}
        for name, value in [*self.fields, *more.items()]:
            sent.setdefault(name, []).append(value)
        return sent


@pytest.fixture
def matrix(tmp_path):
    (tmp_path / 'data').mkdir()
    users = Users(str(tmp_path / 'data' / 'accounts.db'), GROUPS)
    for name, group in (('jordan', 'admin'), ('panel', 'front')):
        users.add(name, group)
    users.change('jordan', password='correct horse')
    two = Fleet(tmp_path)

    class Wires(dict):
        def __missing__(self, port):  # spotter and the rest are not part of this check
            return httpx.ASGITransport(app=Starlette(routes=[Route('/{path:path}', lambda request: JSONResponse(
                {'error': {'message': 'not here'}}, 503), methods=['GET', 'POST'])]))
    wires = Wires({8442: httpx.ASGITransport(app=two.worker('compute')), 9442: httpx.ASGITransport(app=two.worker('rig'))})
    front = turnstile_to(tmp_path, users, wires, machines={'rig': {'compute': 'http://compute:9442', 'spotter': 'http://compute:9444',
                                                                    'machine_key_file': two.keys['rig']}})
    settings = Settings(compute='http://compute', key=users.new_key('panel'), data=str(tmp_path / 'data'), spotter='http://spotter')
    with TestClient(build_app(settings, door(front, 8442), door(front, 8444))) as browser:
        sign_in(browser, 'jordan')
        yield browser, two


def test_node_form_gives_each_node_its_own_fields_and_the_shared_ones():
    form = {'ctx': ['8192'], 'flag': ['temp', 'threads'], 'value': ['0.3', '4'], 'projector': ['/a/p.gguf'], 'order': ['3'],
            'n:rig:ctx': ['4096'], 'n:rig:flag': ['threads', 'temp'], 'n:rig:value': ['8', '0.9'], 'n:a:b:ctx': ['1']}
    assert matrix_nodes(form) == ['rig', 'a:b']
    rig = node_form(form, 'rig')
    assert rig['ctx'] == ['4096'] and rig['projector'] == ['/a/p.gguf'] and rig['order'] == ['3']
    # Its own flags, then the shared generation flags, so a shared value wins; another node's own flags stay out.
    assert list(zip(rig['flag'], rig['value'])) == [('threads', '8'), ('temp', '0.9'), ('temp', '0.3')]


def test_the_page_has_a_column_for_each_copy_with_shared_settings_once(matrix):
    browser, _ = matrix
    page = browser.get('/models/qwen?node=compute').text
    assert page.count('class="matrix-node') == 2 and 'aria-current="true">compute</a>' in page and '>rig</a>' in page
    form = Form(page, 'data-model-settings')
    sent = dict((name, value) for name, value in form.fields)
    assert sent['ctx'] == '8192' and sent['n:rig:ctx'] == '4096' and sent['n:rig:cpu'] == 'yes' and 'cpu' not in sent
    assert [value for name, value in form.fields if name == 'flag'].count('temp') == 1  # a generation default appears once
    assert 'n:rig:flag' in sent and 'threads' in [value for name, value in form.fields if name == 'n:rig:flag']
    assert '<code class="matrix-flag">--temp</code>' in page and 'Compute locations' not in page
    assert 'id="copy-model"' in page and 'Every compute node already has a copy of this model.' in page


def test_one_node_changed_keeps_the_single_review(matrix):
    browser, two = matrix
    form = Form(browser.get('/models/qwen?node=compute').text, 'data-model-settings').set('ctx', '16384')
    shown = browser.post('/models/qwen?node=compute', data=form.data(do='setup-preview'))
    assert shown.status_code == 200, shown.text
    assert 'value="apply-reviewed"' in shown.text and 'apply-reviewed-nodes' not in shown.text


def test_another_nodes_column_is_reviewed_and_applied_on_that_node_alone(matrix):
    browser, two = matrix
    page = browser.get('/models/qwen?node=compute').text
    form = Form(page, 'data-model-settings').set('n:rig:ctx', '2048')
    reviewed = browser.post('/models/qwen?node=compute', data=form.data(do='setup-preview'))
    assert reviewed.status_code == 200, reviewed.text
    assert 'value="apply-reviewed-nodes">Apply to rig</button>' in reviewed.text
    applied = browser.post('/models/qwen?node=compute', data=Form(reviewed.text, 'data-apply-nodes').data(do='apply-reviewed-nodes'))
    assert 'Configuration applied on rig.' in applied.text, applied.text
    assert two.models['rig']['qwen']['flags']['ctx-size'] == 2048 and two.models['rig']['qwen']['flags']['threads'] == 8
    assert two.models['compute']['qwen']['flags']['ctx-size'] == 8192


def test_a_shared_change_and_a_local_one_are_saved_on_every_copy_together(matrix):
    browser, two = matrix
    form = Form(browser.get('/models/qwen?node=compute').text, 'data-model-settings')
    form.fields[form.value_of('flag', 'temp')][1] = '0.2'
    form.set('n:rig:ctx', '1024')
    reviewed = browser.post('/models/qwen?node=compute', data=form.data(do='setup-preview'))
    assert 'Apply to 2 nodes' in reviewed.text, reviewed.text
    applied = browser.post('/models/qwen?node=compute', data=Form(reviewed.text, 'data-apply-nodes').data(do='apply-reviewed-nodes'))
    assert 'Configuration applied on compute and rig.' in applied.text, applied.text
    assert two.models['compute']['qwen']['flags']['temp'] == two.models['rig']['qwen']['flags']['temp'] == .2
    assert two.models['rig']['qwen']['flags']['ctx-size'] == 1024 and two.models['rig']['qwen']['gpus'] == 0


def test_a_shared_file_another_node_lacks_is_refused_before_any_preview(matrix):
    browser, two = matrix
    two.files['rig'] = ('m.gguf',)
    form = Form(browser.get('/models/qwen?node=compute').text, 'data-model-settings').set('projector', '/compute/cache/p.gguf')
    refused = browser.post('/models/qwen?node=compute', data=form.data(do='setup-preview'))
    assert refused.status_code == 400 and 'rig has no copy of /compute/cache/p.gguf. Copy it there in Files first.' in refused.text


def test_a_move_takes_one_destination(matrix):
    browser, _ = matrix
    refused = browser.post('/models/qwen?node=compute', data={'do': 'location-add', 'transfer': 'move', 'machine': ['rig', 'other']})
    assert refused.status_code == 400 and 'A move takes the model to one node.' in refused.text
    nothing = browser.post('/models/qwen?node=compute', data={'do': 'location-add', 'transfer': 'copy'})
    assert nothing.status_code == 400 and 'Choose a node to copy the model to.' in nothing.text
