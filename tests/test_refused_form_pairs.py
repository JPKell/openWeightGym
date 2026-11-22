"""A refused form comes back with each repeated field's own value, so a corrected retry saves what was typed (QA-061)."""

import re
from html.parser import HTMLParser

from loadcoach.models import Models
from loadcoach.roles import Roles
from test_app import gguf
from test_model_matrix import Form, matrix  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


class Sent(HTMLParser):
    """The pairs a browser sends from one form, in document order: no template rows, unticked boxes or unclicked buttons."""

    def __init__(self, html, form_id, button):
        super().__init__()
        self.form_id, self.button, self.pairs, self.inside, self.template, self.select = form_id, button, [], False, 0, None
        self.feed(html)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == 'template':
            self.template += 1
        if tag == 'form':
            self.inside = attrs.get('id') == self.form_id
        name = attrs.get('name')
        if self.template or not name or 'disabled' in attrs or not (self.inside or attrs.get('form') == self.form_id):
            if tag == 'option' and self.select is not None and ('selected' in attrs or self.select[1] is None):
                self.select[1] = attrs.get('value', '')
            return
        kind = attrs.get('type', 'text')
        if tag == 'input' and kind in ('checkbox', 'radio') and 'checked' not in attrs:
            return
        if tag == 'button' or (tag == 'input' and kind == 'submit'):
            if (name, attrs.get('value', '')) == self.button:
                self.pairs.append(self.button)
            return
        if tag == 'input':
            self.pairs.append((name, attrs.get('value', 'on' if kind in ('checkbox', 'radio') else '')))
        elif tag == 'select':
            self.select = [name, None]
        elif tag == 'textarea':
            self.pairs.append((name, ''))

    def handle_endtag(self, tag):
        if tag == 'template':
            self.template -= 1
        elif tag == 'select' and self.select is not None:
            self.pairs.append((self.select[0], self.select[1] or ''))
            self.select = None


def test_a_refused_role_keeps_each_candidates_weight_and_the_add_choice_empty(desk, tmp_path):
    browser, _, _ = desk
    kept = Models(str(tmp_path / "loadcoach.db"))
    for name in ("small", "large"):
        kept.put(name, {"gpus": 1, "flags": {"model": gguf(tmp_path / f"{name}.gguf"), "ctx-size": 2048}})
    roles = Roles(str(tmp_path / "data" / "roles.db"))
    roles.put("chat", {"models": {"small": 0.25, "large": 2}})
    sign_in(browser, "jordan")
    shown = browser.get("/roles", params={"role": "chat"}).text
    pairs = [(name, '' if name == 'loaded_bonus' else value) for name, value in Sent(shown, 'role-editor', ('do', 'save')).pairs]
    assert ('do', 'save') in pairs and ('loaded_bonus', '') in pairs
    refused = browser.post("/roles", content='&'.join(f'{name}={value}' for name, value in pairs),
                           headers={'content-type': 'application/x-www-form-urlencoded'})
    assert refused.status_code == 400 and roles.get("chat")["models"] == {"small": 0.25, "large": 2}
    page = refused.text
    assert re.search(r'id="candidate-1-weight"[^>]*value="2"', page) and re.search(r'id="candidate-2-weight"[^>]*value="0.25"', page)
    assert re.findall(r'<input type="hidden" name="model" value="([^"]*)">', page)[:2] == ["large", "small"]
    add = page[page.index('id="add-role-model"'):]
    assert not re.search(r'<option value="(small|large)"[^>]*selected', add[:add.index('</select>')])  # Add a candidate stays unchosen
    assert re.search(r'id="new-weight"[^>]*value="1"', page)  # the row template is left as it was rendered
    assert re.search(r'id="new-role"[^>]*value=""', page) or not re.search(r'id="new-role"[^>]*value="chat"', page)  # another form is left alone


def test_a_refused_preset_keeps_each_flag_beside_its_own_value(matrix):
    """A refused preset save returns the form with every flag still beside what was typed for it."""
    browser, _ = matrix
    form = Form(browser.get('/models/qwen?node=compute').text, 'data-model-settings')
    flags = [value for name, value in form.fields if name == 'flag']
    assert len(flags) > 2 and 'temp' in flags
    form.fields[form.value_of('flag', 'temp')][1] = '0.3'
    sent = form.data(do='save-preset', preset=' ')
    refused = browser.post('/models/qwen?node=compute', data=sent)
    assert refused.status_code == 400 and 'Give the preset a name.' in refused.text
    back = Form(refused.text, 'data-model-settings')
    assert [value for name, value in back.fields if name == 'flag'] == flags
    assert back.fields[back.value_of('flag', 'temp')][1] == '0.3'
    assert [value for name, value in back.fields if name == 'value'] == sent['value']
