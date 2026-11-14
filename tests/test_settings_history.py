"""Settings transfers validate all values before writes and keep secrets out of evidence."""
import json
import re

import pytest

from loadcoach.settings_transfer import export_rows, history_rows, parse
from test_weightroom import desk, sign_in


def document(*rows):
    return json.dumps({'version':1, 'overrides':list(rows)})


def row(name='mock_input_rate', value=3, node='front', program='weightroom'):
    return {'node':node, 'program':program, 'name':name, 'value':value}


@pytest.mark.parametrize('text', [
    '{}', '{"version":true,"overrides":[]}', 'not JSON',
    document(row(value=-1)), document(row(name='host',value='0.0.0.0')),
    document(row(node='missing')), document(row(program='loadcoach')),
    document(row(), row()), document(row(name='profiles', program='voice', node='compute', value={'default':{'api_key':'hidden'}})),
])
def test_import_refuses_invalid_scope_range_duplicates_and_secret_tables(text):
    with pytest.raises(ValueError):
        parse(text, {'front','compute'})


def test_transfer_preserves_unlisted_values_and_ignores_retired_rows():
    groups, ignored = parse(document(row(), row(name='compute', value='http://retired')), {'front'})
    assert groups == {('front','weightroom'):{'mock_input_rate':3}}
    assert ignored == [{'node':'front', 'program':'weightroom', 'name':'compute'}]
    values = export_rows('front', [{'program':'weightroom','settings':[
        {'name':'mock_input_rate','changed':{'value':3}},
        {'name':'compute','changed':{'value':'http://retired'}},
        {'name':'site_banner','changed':{'value':{'password':'hidden'}}},
    ]}])
    assert values == [row()]


def test_history_has_explicit_legacy_and_secret_states():
    events = [{'id':1,'at':'2026-11-14T01:00:00','kind':'settings','what':'changed','user':'operator',
               'program':'weightroom','mock_input_rate':3},
              {'id':2,'at':'2026-11-14T01:00:01','kind':'settings','what':'changed','program':'machines:compute',
               'old':{'wake_mac':None}, 'wake_mac':'00:11:22:33:44:55'},
              {'id':3,'at':'2026-11-14T01:00:02','kind':'settings','what':'changed','program':'voice',
               'machine':'compute', 'old':{'profiles':{'default':{'token':'hidden'}}},
               'profiles':{'default':{'token':'new-hidden'}}}]
    rows = history_rows(events)
    assert not rows[0]['old_known'] and not rows[0]['can_revert']
    assert rows[1]['node'] == 'compute' and rows[1]['program'] == 'machines' and rows[1]['can_revert']
    assert not rows[2]['can_revert'] and 'hidden' not in json.dumps(rows)


def test_preview_confirm_history_revert_and_export_use_validated_service_posts(desk):
    browser, switch, _ = desk
    changes = switch.front.state.console_settings.changes
    sign_in(browser, 'jordan')
    assert browser.post('/settings/weightroom', data={'value-mock_input_rate':'3'}).status_code == 200
    assert changes.of('weightroom')['mock_input_rate'] == 3
    original = switch.front.state.log.read(kind='settings', what='changed')[-1]
    assert original['old']['mock_input_rate'] is None
    exported = browser.get('/settings/history?export=json')
    assert exported.status_code == 200 and 'attachment' in exported.headers['content-disposition']
    assert row(value=3.0) in exported.json()['overrides']
    text = document(row(value=4))
    preview = browser.post('/settings/history', data={'do':'preview','document':text})
    assert preview.status_code == 200 and 'Preview validated. Nothing has been written.' in preview.text
    assert 'Confirm import' in preview.text and changes.of('weightroom')['mock_input_rate'] == 3
    confirmed = browser.post('/settings/history', data={'do':'confirm','document':text})
    assert confirmed.status_code == 200 and 'Imported 1 settings.' in confirmed.text
    assert changes.of('weightroom')['mock_input_rate'] == 4
    event = switch.front.state.log.read(kind='settings', what='changed')[-1]
    assert event['old']['mock_input_rate'] == 3
    reverted = browser.post('/settings/history', data={'do':'revert','entry':event['id'],'name':'mock_input_rate'})
    assert reverted.status_code == 200 and changes.of('weightroom')['mock_input_rate'] == 3
    reset = browser.post('/settings/history', data={'do':'revert','entry':original['id'],'name':'mock_input_rate'})
    assert reset.status_code == 200 and 'mock_input_rate' not in changes.of('weightroom')


def test_import_checks_every_row_and_never_echoes_rejected_secret_input(desk):
    browser, switch, _ = desk
    changes = switch.front.state.console_settings.changes
    sign_in(browser, 'jordan')
    text = document(row(), row(name='mock_output_rate',value=-1))
    for action in ('preview','confirm'):
        response = browser.post('/settings/history', data={'do':action,'document':text})
        assert response.status_code == 400
        assert not changes.of('weightroom')
    secret = 'must-never-appear-in-response'
    response = browser.post('/settings/history', data={'do':'preview','document':document(row(name='access_token', value=secret))})
    assert response.status_code == 400 and secret not in response.text
    assert secret not in json.dumps(switch.front.state.log.read(kind='settings'))


def test_import_runs_authority_validation_for_all_groups_before_any_write(desk):
    browser, switch, _ = desk
    changes = switch.front.state.console_settings.changes
    changes.seen('turnstile', {'preferred_voice_node':{'value':'', 'file':''}})
    sign_in(browser, 'jordan')
    text = document(row(), row(program='turnstile',name='preferred_voice_node',value='not-an-installed-voice-node'))
    response = browser.post('/settings/history', data={'do':'confirm','document':text})
    assert response.status_code == 400
    assert not changes.of('weightroom') and not changes.of('turnstile')


def test_existing_nested_secret_overrides_are_redacted_without_rewriting_storage(tmp_path):
    from loadcoach.settings import Changes
    changes = Changes(tmp_path/'settings.db')
    value = {'default':{'model':'small', 'token':'never-echo-this'}}
    changes.put('voice','profiles',value,'operator')
    changes.seen('voice', {'profiles':{'value':value}})
    assert 'never-echo-this' not in json.dumps(changes.everything())
    assert changes.of('voice')['profiles'] == value
    from loadcoach.setting_catalog import validate_patch
    with pytest.raises(ValueError, match='write-only'):
        validate_patch('voice', {}, {'profiles':value})
    changes.db.close()


def test_node_import_validates_without_applying_then_changes_live_and_records_old(desk, tmp_path):
    from loadcoach.settings import Changes
    browser, switch, _ = desk
    node_changes = Changes(tmp_path/'loadcoach.db')
    node_changes.seen('loadcoach', {'load_timeout':{'value':300, 'file':300}})
    sign_in(browser, 'jordan')
    text = document(row(name='load_timeout', value=321, node='compute', program='loadcoach'))
    preview = browser.post('/settings/history', data={'do':'preview','document':text})
    assert preview.status_code == 200 and 'Nothing has been written.' in preview.text, re.findall(r'<[^>]*said-box[^>]*>(.*?)</div>', preview.text, re.S)
    # The running Config is reported through the real signed node route.
    inspected = browser.get('/settings/machine?machine=compute').text
    assert 'Site override: <code>321</code>' not in inspected
    confirmed = browser.post('/settings/history', data={'do':'confirm','document':text})
    assert confirmed.status_code == 200 and 'Imported 1 settings.' in confirmed.text
    assert 'Site override: <code>321</code>' in browser.get('/settings/machine?machine=compute').text
    assert node_changes.of('loadcoach')['load_timeout'] == 321
    node_changes.db.close()


def test_history_and_import_require_settings_permission(desk):
    browser, _, _ = desk
    sign_in(browser, 'pat')
    assert browser.get('/settings/history').status_code == 403
    assert browser.post('/settings/history', data={'do':'confirm','document':document(row())}).status_code == 403


@pytest.mark.parametrize('width', [1440, 390])
def test_history_preview_error_and_polling_preserve_unsent_import(desk, width):
    from test_console_browser import browser_context, playwright
    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width,'height':1000})
        page.clock.install()
        page.goto('http://console.test/settings/history')
        text = document(row())
        field = page.get_by_label('Import JSON', exact=True)
        field.fill(text)
        page.clock.fast_forward(15001)
        playwright.expect(field).to_have_value(text)
        page.get_by_role('button', name='Preview import', exact=True).click()
        playwright.expect(page.get_by_role('button', name='Confirm import', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_label('Import JSON', exact=True).fill(document(row(value=-1)))
        page.get_by_role('button', name='Preview import', exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('mock_input_rate')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()
