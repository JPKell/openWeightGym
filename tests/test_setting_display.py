"""Display, pricing and audio defaults change within the same running app."""
import html
import json
import re

from test_weightroom import desk, sign_in


def audio_defaults(text):
    return json.loads(html.unescape(re.search(r'data-defaults="([^"]*)"', text)[1]))


def rate(text, name):
    return float(re.search(r'name="' + name + r'"[^>]*value="([^"]*)"', text)[1])


def test_time_zone_updates_templates_and_resets_without_restart(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    for zone in ('America/Toronto', 'America/Vancouver'):
        response = browser.post('/settings/weightroom', data={'value-timezone': json.dumps(zone)})
        assert response.status_code == 200 and 'Saved.' in response.text
        assert f'data-input-zone="{zone}"' in browser.get('/log?part=page').text
        assert f'data-input-zone="{zone}"' in browser.get('/requests?part=page').text
    invalid = browser.post('/settings/weightroom', data={'value-timezone': '"Unknown/Invalid"'})
    assert invalid.status_code == 400
    assert 'data-input-zone="America/Vancouver"' in browser.get('/log?part=page').text
    assert browser.post('/settings/weightroom', data={'reset': 'timezone'}).status_code == 200
    assert 'data-input-zone="UTC"' in browser.get('/log?part=page').text


def test_mock_rates_apply_to_usage_defaults_and_reject_out_of_range_values(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    response = browser.post('/settings/weightroom', data={
        'value-mock_input_rate': '3.25', 'value-mock_output_rate': '12.5',
    })
    assert response.status_code == 200
    page = browser.get('/usage?part=page').text
    assert rate(page, 'input_rate') == 3.25 and rate(page, 'output_rate') == 12.5
    for name, value in [('mock_input_rate', '-1'), ('mock_output_rate', '1000001')]:
        refused = browser.post('/settings/weightroom', data={'value-' + name: value})
        assert refused.status_code == 400
    for name in ('mock_input_rate', 'mock_output_rate'):
        assert browser.post('/settings/weightroom', data={'reset': name}).status_code == 200
    page = browser.get('/usage?part=page').text
    assert rate(page, 'input_rate') == 2 and rate(page, 'output_rate') == 10


def test_all_audio_defaults_reach_new_page_requests_without_restart(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    chosen = {kind: 'catalogue-' + kind for kind in ('lyrics', 'music', 'sound', 'speech', 'transcription')}
    response = browser.post('/system/audio', data={'value-audio_' + kind + '_model': value for kind, value in chosen.items()})
    assert response.status_code == 200 and 'Applied immediately.' in response.text
    for path in ('/chat/music', '/chat/sounds', '/chat/speech', '/chat/transcribe'):
        assert audio_defaults(browser.get(path).text) == chosen
    for kind in chosen:
        assert browser.post('/settings/weightroom', data={'reset': 'audio_' + kind + '_model'}).status_code == 200
    assert audio_defaults(browser.get('/chat/music').text) == {
        'lyrics': 'chat', 'music': '', 'sound': '', 'speech': '', 'transcription': '',
    }
