"""Checks of Cloud APIs: an outside service registered, its models chosen and priced, from the site."""

from loadcoach.outside import Services
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)
from test_outside import service  # noqa: F401


def test_the_cloud_apis_page_registers_a_service_and_its_models_are_ticked_from_its_own_list(desk, service, tmp_path, monkeypatch):
    browser, _, _ = desk
    kept = Services(str(tmp_path / "data" / "accounts.db"), str(tmp_path))  # the services and their keys are front's, beside the machine key
    sign_in(browser, "pat")
    assert browser.get("/cloud-apis").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, "sam")  # someone who can do everything: adding a service says where requests may leave to
    page = browser.get("/cloud-apis")
    assert 'href="/cloud-apis"' in browser.get("/models").text
    assert "Cloud APIs" in page.text and "No cloud API is configured." in page.text
    assert "A service somewhere else that answers" not in page.text
    moved = browser.get("/outside", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/cloud-apis"

    wrong = browser.post("/cloud-apis", data={"do": "add", "name": "Rented One", "url": service})
    assert wrong.status_code == 400 and "lowercase letters, digits and dashes" in wrong.text
    added = browser.post("/cloud-apis", data={"do": "add", "name": "rented", "url": service, "ceiling": "1.5"})
    assert "Added rented." in added.text and "No API key installed." in added.text and 'type="password"' in added.text
    assert "did not give a list of its models" in browser.get("/cloud-apis?offers=rented").text
    installed = browser.post("/cloud-apis", data={"do": "key", "name": "rented", "api_key": "good-key"})
    assert "API key installed." in installed.text and "good-key" not in installed.text
    offers = browser.get("/cloud-apis?offers=rented").text
    assert "API key installed." in offers and 'name="offer" value="vendor/big"' in offers and 'name="offer" value="vendor/small"' in offers

    # Ticked in the service's own list, a model is offered under the service's name for it. Then it is given a name of ours and a price.
    browser.post("/cloud-apis", data={"do": "offer", "name": "rented", "offer": ["vendor/big"]})
    assert kept.get("rented") == {"url": service, "models": {"vendor/big": {"theirs": "vendor/big"}}, "ceiling": 1.5}
    assert 'value="vendor/big" type="checkbox" class="form-check-input" checked disabled' in browser.get("/cloud-apis?offers=rented").text
    browser.post("/cloud-apis", data={"do": "models", "name": "rented", "ours": ["big-out", ""], "theirs": ["vendor/big", ""], "in": ["2", ""], "out": ["6.5", ""]})
    assert kept.get("rented")["models"] == {"big-out": {"theirs": "vendor/big", "in": 2, "out": 6.5}}
    shown = browser.get("/cloud-apis").text
    assert 'name="ours" class="form-control" value="big-out"' in shown and "1 model" in shown and "$0.00 in the last 24 hours of $1.50" in shown

    browser.post("/cloud-apis", data={"do": "save", "name": "rented", "url": service, "ceiling": ""})  # the ceiling emptied: none. Its models are as they were
    assert "ceiling" not in kept.get("rented") and list(kept.get("rented")["models"]) == ["big-out"]
    removed = browser.post("/cloud-apis", data={"do": "remove", "name": "rented"})
    assert "Removed rented." in removed.text and kept.get("rented") is None


def test_account_desk_section_saves_preserve_other_settings_and_same_endpoint_accounts(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, 'sam')
    kept = Services(str(tmp_path / 'data/accounts.db'), str(tmp_path))
    for name in ('work', 'personal'):
        browser.post('/cloud-apis', data={'do':'add', 'name':name, 'url':'https://example.invalid/v1', 'ceiling':'25'})
    changed = browser.post('/cloud-apis?section=connection', data={'do':'save', 'name':'work', 'url':'https://example.invalid/new/v1'})
    assert changed.status_code == 200 and 'data-cloud-account="work"' in changed.text
    assert kept.get('work')['ceiling'] == 25
    browser.post('/cloud-apis?section=limits', data={'do':'save', 'name':'work', 'ceiling':'42'})
    assert kept.get('work') == {'url':'https://example.invalid/new/v1', 'models':{}, 'ceiling':42}
    assert kept.get('personal')['ceiling'] == 25 and kept.get('personal')['url'] == 'https://example.invalid/v1'
    browser.post('/cloud-apis?section=limits', data={'do':'save', 'name':'work', 'ceiling':''})
    assert 'ceiling' not in kept.get('work')
    page = browser.get('/cloud-apis?account=personal&section=limits').text
    assert 'data-cloud-account="personal"' in page and 'data-cloud-section="limits"' in page
    removal = page.split('id="remove-form"')[1].split('</form>')[0]
    assert 'data-confirm-two' in removal and 'data-loss-confirm' not in removal
