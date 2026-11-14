"""Alert evidence explains its measurement and links to the actual investigation path."""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from weightroom.alerts import Alerts, observation_conditions
from weightroom.alert_guidance import investigation
from weightroom.display import timestamp
from test_weightroom import desk, sign_in  # noqa: F401


def report():
    return dict(local={'process':'front', 'recording':True, 'dropped':{}}, groups=[],
                sources=[dict(machine='compute', process='worker', recording=True, received=40,
                              fresh=False, dropped={})])


def find(readings, suffix):
    return next(row for row in readings if row['key'] == 'observation:compute:' + suffix)


def test_stale_report_describes_front_receipt_and_recording_without_inventing_cause():
    rows, _ = observation_conditions(report(), now=100)
    row = find(rows, 'freshness')
    assert row['active'] is True
    for text in ('LoadCoach on node compute', 'front\'s Turnstile', 'POST /turnstile/log',
                 '60.0 seconds before this check', 'less than 60 seconds', 'recording=true',
                 'does not prove that a service crashed'):
        assert text in row['explanation']


def test_recent_receipt_with_recording_off_is_distinct_from_stale_delivery():
    data = report()
    data['sources'][0].update(received=99, recording=False)
    rows, _ = observation_conditions(data, now=100)
    explanation = find(rows, 'freshness')['explanation']
    assert '1.0 seconds before this check' in explanation and 'recording=false' in explanation
    data['sources'][0].pop('received')
    rows, _ = observation_conditions(data, now=100)
    assert 'did not provide a last health receipt time' in find(rows, 'freshness')['explanation']
    assert '1970' not in find(rows, 'freshness')['explanation']


def test_loss_backlog_and_threshold_messages_retain_measured_values_and_rules():
    data = report()
    data['sources'][0].update(fresh=True, backlog_count=12, backlog_age_seconds=80,
                              dropped={'writer_error':3, 'sampled_success':99})
    data.update(since=0, until=300, groups=[dict(service='turnstile', operation='ingress',
                metric='seconds', count=200, outcomes={'error':30}, p95=5)])
    rows, previous = observation_conditions(data, now=100, error_percent=10, p95_seconds=2)
    assert '12 observations' in find(rows, 'backlog')['explanation']
    assert '80 seconds old' in find(rows, 'backlog')['explanation']
    assert 'loss count 3' in find(rows, 'loss')['explanation']
    assert 'writer_error=3' in find(rows, 'loss')['explanation']
    error = next(row for row in rows if row['key'].endswith(':error-rate'))['explanation']
    assert '30 recorded errors / 200 ingress calls = 15.00%' in error
    assert 'configured threshold 10%' in error and 'Window: 300 seconds' in error
    latency = next(row for row in rows if row['key'].endswith(':latency'))['explanation']
    assert 'p95 5 seconds' in latency and 'threshold 2 seconds' in latency
    data['sources'][0].update(process='replacement', dropped={'writer_error':1})
    rows, _ = observation_conditions(data, previous, now=101)
    assert 'previous count for this process 0' in find(rows, 'loss')['explanation']


def test_old_episode_gets_guidance_without_changing_its_recorded_evidence():
    row = dict(source='observability', condition='observation:compute:freshness',
               explanation='Old generic explanation')
    guide = investigation(row)
    assert row['explanation'] == 'Old generic explanation'
    assert 'GET /loadcoach/observation-metrics' in guide['detection']
    assert 'Every 30 seconds WeightRoom reads' in guide['detection']
    assert 'loadcoach.service' in guide['services'] and 'turnstile.service' in guide['services']
    assert any(step['href'] == '/output?node=compute&source=loadcoach' for step in guide['steps'])
    target = guide['steps'][0]['href']
    assert urlsplit(target).fragment == 'observation-metrics'
    assert parse_qs(urlsplit(target).query)['complete'] == ['1']
    assert any('turnstile.service' in command['command'] for command in guide['commands'])
    local = investigation(dict(source='observability', condition='observation:front:freshness'))
    assert 'local writer health' in local['detection']
    assert not any('/output?node=front' in step['href'] for step in local['steps'])


def test_active_episode_message_refresh_preserves_identity_and_acknowledgement(tmp_path):
    store = Alerts(tmp_path/'alerts.db')
    store.observe('observability', [dict(key='observation:compute:freshness', active=True,
                  title='Observation source unavailable · compute', explanation='Old generic explanation')], now=90)
    original = store.rows(open_only=True)[0]
    store.acknowledge(original['id'], 'jordan')
    rows, _ = observation_conditions(report(), now=100)
    store.observe('observability', rows, now=100, missing_recovers=False)
    refreshed = store.rows(open_only=True)[0]
    assert refreshed['id'] == original['id'] and refreshed['first_at'] == 90
    assert refreshed['acknowledged_by'] == 'jordan'
    assert 'Front last received' in refreshed['explanation']
    store.db.close()


@pytest.mark.parametrize('source,condition,target', [
    ('services','service:compute:loadcoach.service','/output'),
    ('GPU temperature','gpu:compute:0','/telemetry'),
    ('memory kills','compute:oom:kernel:cursor','/output?source=kernel'),
    ('model failures','model:["id","fingerprint","chat"]','/reliability'),
    ('spending','spend:service:provider','/cloud-apis'),
])
def test_each_alert_family_has_detection_services_and_investigation_links(source, condition, target):
    guide = investigation(dict(source=source, condition=condition))
    assert guide['detection'] and guide['services']
    assert target in [step['href'] for step in guide['steps']]


def test_investigation_markup_escapes_labels_and_does_not_offer_forbidden_links():
    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root/'weightroom/pages'), autoescape=select_autoescape())
    env.filters['timestamp'] = timestamp
    guide = investigation(dict(source='observability', condition='observation:<script>unsafe</script>:freshness'))
    markup = str(env.get_template('alert_parts.html').module.investigation(guide, ['manage-settings']))
    assert '<script>unsafe</script>' not in markup and '&lt;script&gt;' in markup
    assert 'href="/output' not in markup and 'requires restart-services' in markup
    assert 'href="/requests' not in markup and 'requires see-log' in markup


@pytest.mark.parametrize('width', [1440, 390])
def test_live_stale_alert_links_to_open_metrics_and_shows_recorded_health(desk, width):
    from test_console_browser import browser_context, playwright
    server, switch, _ = desk
    switch.off = True
    received = time.time() - 600
    with switch.front.state.log.db:
        switch.front.state.log.db.execute('INSERT INTO observation_sources(machine,at,data) VALUES(?,?,?) '
            'ON CONFLICT(machine) DO UPDATE SET at=excluded.at,data=excluded.data',
            ('compute', received, json.dumps(dict(process='fixture', recording=True, dropped={},
                                                backlog_count=2, backlog_age_seconds=80))))
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server)
        page.set_viewport_size({'width':width,'height':1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/alerts?source=observability&q=compute')
        playwright.expect(page.locator('#inspector [data-explanation]')).to_contain_text('Front last received')
        playwright.expect(page.locator('[data-related-services]')).to_contain_text('loadcoach.service')
        page.get_by_text('Host journal commands', exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.get_by_role('link', name='Model requests → Operational metrics', exact=True).click()
        playwright.expect(page.locator('#observation-metrics')).to_have_attribute('open', '')
        playwright.expect(page.locator('[data-observation-source="compute"]')).to_contain_text('recording On')
        playwright.expect(page.locator('[data-observation-source="compute"]')).to_contain_text('oldest unforwarded age 80')
        assert page.locator('[data-observation-source="compute"] time').get_attribute('datetime')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
