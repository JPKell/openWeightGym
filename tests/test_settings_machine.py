"""Read-only configuration shows real source/default evidence without exposing secrets."""
import json

import pytest

from loadcoach.settings import Changes, said
from test_weightroom import desk, sign_in


def test_source_and_default_metadata_distinguish_machine_defaults_and_builtins(tmp_path):
    defaults = tmp_path/'defaults'
    defaults.mkdir()
    (defaults/'spotter.toml').write_text('sample_every = 30\nkeep_days = 28\n')
    machine = tmp_path/'spotter.toml'
    machine.write_text('sample_every = 45\nkey = "never-show-this"\n')
    report = said({'sample_every':45, 'keep_days':28, 'host':'127.0.0.1', 'key':'never-show-this'},
                  {'sample_every':45, 'keep_days':28}, {}, 'spotter', path=machine,
                  defaults={'host':'127.0.0.1'})
    assert report['sample_every']['source'] == str(machine)
    assert report['sample_every']['default'] == 30 and report['sample_every']['differs']
    assert report['keep_days']['source'] == str(defaults/'spotter.toml')
    assert not report['keep_days']['differs']
    assert report['host']['source'] == 'Built-in service value'
    assert 'never-show-this' not in json.dumps(report)
    changes = Changes(tmp_path/'settings.db')
    changes.seen('spotter', {'access_token': {'value':'secret', 'file':'secret', 'default':'secret'}})
    assert 'secret' not in json.dumps(changes.everything())
    changes.db.close()


def test_turnstile_source_metadata_reads_its_section_and_nested_voice_defaults(tmp_path):
    (tmp_path/'defaults').mkdir()
    (tmp_path/'defaults/weightroom.toml').write_text('timezone = "UTC"\n[turnstile]\nwait = 180\n')
    path = tmp_path/'weightroom.toml'
    path.write_text('[turnstile]\nwait = 190\n')
    report = said({'wait':190}, {'wait':190}, {}, 'turnstile', path=path)
    assert report['wait']['default'] == 180 and report['wait']['differs']
    assert report['wait']['source'] == str(path)
    voice = tmp_path/'voice.toml'
    voice.write_text('[recognition]\nthreads = 6\n')
    (tmp_path/'defaults/voice.toml').write_text('[recognition]\nthreads = 4\n')
    report = said({'recognition':{'threads':6}}, {'recognition':{'threads':6}}, {}, 'voice', path=voice)
    assert report['recognition.threads']['default'] == 4
    assert report['recognition.threads']['source'] == str(voice)


def test_wake_file_source_and_defaults_belong_to_the_control_node(tmp_path):
    path = tmp_path/'weightroom.toml'
    path.write_text('[machines.rig]\nwake_mac="00:11:22:33:44:55"\n')
    report = said({'wake_mac':'00:11:22:33:44:55', 'wake_address':'255.255.255.255'},
                  {'wake_mac':'00:11:22:33:44:55', 'wake_address':'255.255.255.255'},
                  {}, 'machines:rig', path=path)
    assert report['wake_mac']['source'] == str(path)
    assert report['wake_mac']['default'] == '' and report['wake_mac']['differs']
    assert report['wake_address']['default'] == '255.255.255.255'
    assert not report['wake_address']['differs']


def test_machine_page_is_read_only_scoped_and_shows_retired_values(desk):
    import time
    browser, switch, _ = desk
    changes = switch.front.state.console_settings.changes
    for _ in range(100):
        if any(program['program'] == 'weightroom' for program in changes.everything()):
            break
        time.sleep(.01)
    changes.seen('weightroom', {
        'compute': {'value':'http://old-compute', 'file':'http://old-compute', 'source':'/settings/weightroom.toml'},
        'secure': {'value':True, 'file':True, 'default':False, 'differs':True, 'source':'/settings/weightroom.toml'},
    })
    sign_in(browser, 'jordan')
    normal = browser.get('/settings').text
    assert 'http://old-compute' not in normal
    response = browser.get('/settings/machine')
    assert response.status_code == 200
    assert 'http://old-compute' in response.text and 'Retired · ignored' in response.text
    assert '/settings/weightroom.toml' in response.text and 'Differs from default' in response.text
    assert 'owg restart weightroom' in response.text
    assert 'name="value-secure"' not in response.text
    security = browser.get('/settings/security')
    assert security.status_code == 200 and 'secure' in security.text and '/settings/weightroom.toml' in security.text
    assert browser.get('/settings/machine?machine=missing').status_code == 404
    wake = browser.get('/settings/machine?machine=compute')
    assert wake.status_code == 200
    assert 'Turnstile on the control node' in wake.text and '[machines.compute]' in wake.text
    assert 'owg restart machines' not in wake.text
    sign_in(browser, 'pat')
    assert browser.get('/settings/machine').status_code == 403


@pytest.mark.parametrize('width', [1440, 390])
def test_machine_selector_filter_and_empty_state_fit(desk, width):
    from test_console_browser import browser_context, playwright
    server, switch, _ = desk
    switch.front.state.console_settings.changes.seen('turnstile', {
        'host':{'value':'127.0.0.1', 'file':'127.0.0.1', 'source':'/settings/weightroom.toml', 'default':'0.0.0.0', 'differs':True},
        'ports':{'value':[8442,8443], 'file':[8442,8443]},
    })
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width, 'height':1000})
        page.goto('http://console.test/settings/machine')
        page.get_by_label('Find a setting', exact=True).fill('host')
        playwright.expect(page.get_by_text('Differs from default', exact=True)).to_be_visible()
        playwright.expect(page.locator('tr').filter(has=page.locator('code', has_text='ports')).first).to_be_hidden()
        page.get_by_label('Find a setting', exact=True).fill('not-a-setting')
        playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
        page.get_by_role('button', name='Clear filters', exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_label('Filter node', exact=True).select_option('compute')
        page.get_by_role('button', name='Inspect', exact=True).click()
        playwright.expect(page).to_have_url('http://console.test/settings/machine?machine=compute')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('input[name^="value-"]').count() == 0
        browser.close()
