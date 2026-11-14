"""Notification transports talk only to disposable local receivers."""
import asyncio
import base64
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socketserver
import ssl
import subprocess
import threading
import time

import pytest

from loadcoach.setting_catalog import get
from weightroom.app import Settings
from weightroom.alerts import Alerts
from weightroom.notify import NotificationKeys,Sender,DeliveryError,Notifications,payload,quiet_now
from test_weightroom import desk,sign_in  # noqa: F401
from test_account_settings import api as account_api
from test_postgresql import postgres  # noqa: F401


@pytest.fixture
def receiver():
    records = []
    class HTTP(BaseHTTPRequestHandler):
        def do_POST(self):
            records.append((self.path,dict(self.headers),self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(302 if self.path == '/redirect' else 503 if self.path=='/failed' else 200)
            if self.path == '/redirect':
                self.send_header('Location','/never-follow')
            self.send_header('Content-Length','0')
            self.end_headers()
        def log_message(self,*args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),HTTP)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield 'http://127.0.0.1:'+str(server.server_port),records
    server.shutdown();server.server_close();thread.join()


def episode():
    return dict(id='episode-test',condition='disk:node:/',source='Disk space',title='Disk nearly full',
                first_at=100,last_at=120,resolved_at=None,explanation='metadata only')


def test_webhook_body_is_signed_and_resolution_metadata_is_complete(receiver,tmp_path):
    url,records = receiver
    settings = Settings(key='test',notify_webhook_url=url+'/hook',notify_site_url='https://console.example.test')
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_webhook_secret','fake-signing-secret')
    sender = Sender(lambda:settings,keys)
    asyncio.run(sender.send('webhook',payload(episode(),settings.notify_site_url)))
    path,headers,body = records[-1]
    assert path == '/hook'
    signature = hmac.new(b'fake-signing-secret',body,hashlib.sha256).hexdigest()
    assert headers['X-Suite-Signature'] == 'sha256='+signature
    sent = json.loads(body)
    assert set(sent) == {'episode_id','condition','source','title','state','first_at','last_at','resolved_at','link'}
    assert sent['state'] == 'opened' and sent['link'].startswith('https://console.example.test/alerts?')
    changed = {**episode(),'resolved_at':180}
    asyncio.run(sender.send('webhook',payload(changed)))
    assert json.loads(records[-1][2])['state'] == 'resolved'
    assert 'explanation' not in body.decode() and 'fake-signing-secret' not in body.decode()
    keys.remove('notify_webhook_secret')
    with pytest.raises(DeliveryError,match='signing secret'):
        asyncio.run(sender.send('webhook',payload(episode())))
    assert len(records) == 2


def test_ntfy_uses_root_json_and_never_borrows_system_tokens_for_personal_channels(receiver,tmp_path):
    url,records = receiver
    settings = Settings(key='test',notify_ntfy_url=url,notify_ntfy_topic='operations')
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_ntfy_token','fake-system-token')
    sender = Sender(lambda:settings,keys)
    asyncio.run(sender.send('ntfy',payload(episode())))
    assert records[-1][0] == '/' and records[-1][1]['Authorization'] == 'Bearer fake-system-token'
    assert json.loads(records[-1][2])['topic'] == 'operations'
    recipient = {'notify_ntfy_url':url,'notify_ntfy_topic':'personal'}
    asyncio.run(sender.send('ntfy',payload(episode()),recipient,'owner-id'))
    assert 'Authorization' not in records[-1][1] and json.loads(records[-1][2])['topic'] == 'personal'
    keys.save('notify_ntfy_token','fake-personal-token','owner-id')
    asyncio.run(sender.send('ntfy',payload(episode(),'https://console.example.test'),recipient,'owner-id'))
    assert records[-1][1]['Authorization'] == 'Bearer fake-personal-token'
    assert json.loads(records[-1][2])['click'].startswith('https://console.example.test/')


@pytest.mark.parametrize('path',['/redirect','/failed'])
def test_http_errors_are_sanitized_and_redirects_are_not_followed(receiver,tmp_path,path):
    url,records = receiver
    settings = Settings(key='test',notify_webhook_url=url+path)
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_webhook_secret','fake-private-key')
    with pytest.raises(DeliveryError) as failed:
        asyncio.run(Sender(lambda:settings,keys).send('webhook',payload(episode())))
    assert len(records) == 1 and url not in str(failed.value) and 'fake-private-key' not in str(failed.value)


@pytest.fixture
def smtp_receiver(tmp_path):
    if not shutil.which('openssl'):
        pytest.skip('openssl is needed for the disposable SMTP TLS receiver')
    key,cert = tmp_path/'smtp.key',tmp_path/'smtp.crt'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                    '-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],
                   check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    servers = []
    def make(implicit):
        captured = {'commands':[],'messages':[]}
        class SMTP(socketserver.BaseRequestHandler):
            def handle(self):
                connection = context.wrap_socket(self.request,server_side=True) if implicit else self.request
                stream = connection.makefile('rb')
                def reply(value):
                    connection.sendall(value.encode()+b'\r\n')
                reply('220 localhost disposable SMTP')
                while line := stream.readline():
                    command = line.split(b' ',1)[0].strip().upper()
                    captured['commands'].append(command.decode())
                    if command == b'EHLO':
                        reply('250-localhost\r\n250-STARTTLS\r\n250 AUTH PLAIN')
                    elif command == b'STARTTLS':
                        reply('220 Begin TLS')
                        stream.close()
                        connection = context.wrap_socket(connection,server_side=True)
                        stream = connection.makefile('rb')
                    elif command == b'AUTH':
                        captured['auth'] = base64.b64decode(line.strip().split()[-1])
                        reply('235 Authenticated')
                    elif command == b'DATA':
                        reply('354 Send data')
                        chunks = []
                        while (data := stream.readline()) != b'.\r\n':
                            if not data:
                                break
                            chunks.append(data)
                        captured['messages'].append(b''.join(chunks))
                        reply('250 Accepted')
                    elif command == b'QUIT':
                        reply('221 Bye')
                        break
                    else:
                        reply('250 OK')
                stream.close()
                connection.close()
        server = socketserver.ThreadingTCPServer(('127.0.0.1',0),SMTP)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        servers.append((server,thread))
        return server.server_address[1],captured,ssl.create_default_context(cafile=str(cert))
    yield make
    for server,thread in servers:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('mode',['tls','starttls'])
def test_smtp_uses_tls_and_sends_only_metadata_to_the_local_receiver(smtp_receiver,tmp_path,mode):
    port,captured,context = smtp_receiver(mode=='tls')
    settings = Settings(key='test',notify_smtp_host='127.0.0.1',notify_smtp_port=port,notify_smtp_security=mode,
                        notify_smtp_username='test-user',notify_smtp_from='sender@example.test',notify_smtp_to='owner@example.test')
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_smtp_password','fake-smtp-password')
    sender = Sender(lambda:settings,keys,smtp_context=context)
    asyncio.run(sender.send('email',payload(episode())))
    assert captured['auth'] == b'\0test-user\0fake-smtp-password'
    assert (b'STARTTLS'.decode() in captured['commands']) == (mode=='starttls')
    message = BytesParser(policy=policy.default).parsebytes(captured['messages'][0])
    assert message['To'] == 'owner@example.test' and message['Subject'] == episode()['title']
    assert '/alerts?' in message.get_content() and 'fake-smtp-password' not in message.as_string()


def test_secrets_are_owner_only_atomic_write_only_and_scoped(tmp_path):
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_ntfy_token','first-private','owner-id')
    first = keys.path('notify_ntfy_token','owner-id')
    assert first.stat().st_mode & 0o777 == 0o600
    keys.save('notify_ntfy_token','','owner-id')
    assert keys.read('notify_ntfy_token','owner-id') == 'first-private'
    keys.save('notify_ntfy_token','second-private','owner-id')
    assert keys.read('notify_ntfy_token','owner-id') == 'second-private'
    assert keys.read('notify_ntfy_token','other-id') == ''
    assert 'private' not in json.dumps(keys.status('notify_ntfy_token','owner-id'))
    assert not list(first.parent.glob('.telephone-*'))
    with pytest.raises(ValueError):
        keys.save('notify_ntfy_token','bad\ncredential','owner-id')
    assert keys.read('notify_ntfy_token','owner-id') == 'second-private'
    keys.remove('notify_ntfy_token','owner-id')
    assert not keys.status('notify_ntfy_token','owner-id')['saved']


def test_durable_delivery_retries_are_bounded_and_survive_reopen(tmp_path):
    settings = Settings(key='test',notify_webhook_url='http://127.0.0.1:1/hook')
    alerts = Alerts(tmp_path/'alerts.db')
    keys = NotificationKeys(tmp_path/'keys')
    class Failure:
        async def send(self,*args):
            raise DeliveryError('Local test failure.')
    worker = Notifications(alerts,lambda:settings,keys,Failure())
    worker.enqueue('example','system','webhook','opened',payload(episode()),100)
    asyncio.run(worker.process(now=100))
    assert worker.status()[0]['attempts'] == 1 and worker.status()[0]['state']=='pending'
    asyncio.run(worker.process(now=159))
    assert worker.status()[0]['attempts'] == 1
    alerts.db.close()
    reopened = Alerts(tmp_path/'alerts.db')
    try:
        worker = Notifications(reopened,lambda:settings,keys,Failure())
        asyncio.run(worker.process(now=160))
        assert worker.status()[0]['attempts'] == 2
        asyncio.run(worker.process(now=460))
        asyncio.run(worker.process(now=10000))
        assert worker.status()[0]['attempts'] == 3 and worker.status()[0]['state']=='failed'
    finally:
        reopened.db.close()


@pytest.mark.parametrize('value',['bad','24:00-01:00','22:00-22:00'])
def test_quiet_hours_validation_comes_from_the_catalogue(value):
    with pytest.raises(ValueError):
        get('weightroom','notify_quiet_hours').check(value)


def test_quiet_hours_include_overnight_intervals_in_control_node_zone():
    settings = Settings(key='test',timezone='America/Vancouver',notify_quiet_hours='22:00-07:00')
    from datetime import datetime
    assert quiet_now(settings,datetime.fromisoformat('2026-11-14T06:00:00+00:00').timestamp())
    assert not quiet_now(settings,datetime.fromisoformat('2026-11-14T16:00:00+00:00').timestamp())



def test_channel_destinations_and_credentials_are_read_on_each_delivery(receiver,smtp_receiver,tmp_path):
    url,records = receiver
    selected = [Settings(key='test',notify_webhook_url=url+'/first')]
    keys = NotificationKeys(tmp_path/'keys')
    keys.save('notify_webhook_secret','fake-first')
    sender = Sender(lambda:selected[0],keys)
    asyncio.run(sender.send('webhook',payload(episode())))
    selected[0] = replace(selected[0],notify_webhook_url=url+'/second')
    keys.save('notify_webhook_secret','fake-second')
    asyncio.run(sender.send('webhook',payload(episode())))
    assert [record[0] for record in records] == ['/first','/second']
    assert records[0][1]['X-Suite-Signature'] != records[1][1]['X-Suite-Signature']
    first,one,context = smtp_receiver(False)
    second,two,_ = smtp_receiver(True)
    sender.smtp_context = context
    selected[0] = replace(selected[0],notify_smtp_host='127.0.0.1',notify_smtp_port=first,notify_smtp_security='starttls',
                          notify_smtp_from='first@example.test',notify_smtp_to='one@example.test',notify_smtp_username='first')
    keys.save('notify_smtp_password','fake-first')
    asyncio.run(sender.send('email',payload(episode())))
    selected[0] = replace(selected[0],notify_smtp_port=second,notify_smtp_security='tls',notify_smtp_from='second@example.test',
                          notify_smtp_to='two@example.test',notify_smtp_username='second')
    keys.save('notify_smtp_password','fake-second')
    asyncio.run(sender.send('email',payload(episode())))
    assert one['auth'] == b'\0first\0fake-first' and two['auth'] == b'\0second\0fake-second'
    assert BytesParser(policy=policy.default).parsebytes(two['messages'][0])['To'] == 'two@example.test'


@pytest.fixture
def delivery_model(tmp_path):
    selected = [Settings(key='test',notify_webhook_url='http://127.0.0.1:1/hook')]
    alerts = Alerts(tmp_path/'alerts.db')
    sent = []
    class Capture:
        async def send(self,*args):
            sent.append(args)
    worker = Notifications(alerts,lambda:selected[0],NotificationKeys(tmp_path/'keys'),Capture())
    yield worker,alerts,selected,sent
    alerts.db.close()


def reading(active,key='disk'):
    return {'key':key,'active':active,'title':'Disk space','explanation':'Recorded metadata'}


def test_episode_open_and_resolve_are_deduplicated_and_quiet_hours_are_live(delivery_model):
    worker,alerts,selected,sent = delivery_model
    now = time.time()+1
    alerts.observe('Disk space',[reading(True)],now)
    worker.plan(now=now)
    worker.plan(now=now)
    asyncio.run(worker.process(now=now))
    assert len(sent)==1 and sent[0][1]['state']=='opened'
    alerts.observe('Disk space',[reading(False)],now+10)
    worker.plan(now=now+10)
    asyncio.run(worker.process(now=now+10))
    worker.plan(now=now+11)
    asyncio.run(worker.process(now=now+11))
    assert [row[1]['state'] for row in sent] == ['opened','resolved']
    # A live quiet-hours change delays an already queued episode without spending an attempt.
    from datetime import datetime,timezone,timedelta
    stamp = datetime.fromtimestamp(now,timezone.utc)
    start = stamp.strftime('%H:%M')
    end = (stamp+timedelta(minutes=5)).strftime('%H:%M')
    selected[0] = replace(selected[0],timezone='UTC',notify_quiet_hours=start+'-'+end)
    alerts.observe('Disk space',[reading(True,'other')],now+12)
    worker.plan(now=now+12)
    asyncio.run(worker.process(now=now+12))
    assert len(sent)==2
    selected[0] = replace(selected[0],notify_quiet_hours='')
    asyncio.run(worker.process(now=now+12))
    assert len(sent)==3


def test_live_delay_repeats_and_acknowledgement_policy(delivery_model):
    worker,alerts,selected,sent = delivery_model
    now = time.time()+1
    selected[0] = replace(selected[0],notify_min_state='unacknowledged',notify_after_minutes=15)
    alerts.observe('Disk space',[reading(True)],now)
    worker.plan(now=now+30)
    assert not worker.status()
    selected[0] = replace(selected[0],notify_after_minutes=1)
    worker.plan(now=now+61)
    asyncio.run(worker.process(now=now+61))
    assert len(sent)==1
    selected[0] = replace(selected[0],notify_repeat_minutes=1)
    worker.plan(now=now+122)
    asyncio.run(worker.process(now=now+122))
    assert sent[-1][1]['state']=='repeat'
    identifier = alerts.rows(open_only=True)[0]['id']
    alerts.acknowledge(identifier,'owner')
    worker.plan(now=now+183)
    asyncio.run(worker.process(now=now+183))
    assert len(sent)==2


def test_concurrent_delivery_polls_claim_once(delivery_model):
    worker,alerts,_,sent = delivery_model
    now = time.time()+1
    alerts.observe('Disk space',[reading(True)],now)
    worker.plan(now=now)
    async def together():
        await asyncio.gather(worker.process(now=now),worker.process(now=now))
    asyncio.run(together())
    assert len(sent)==1 and worker.status()[0]['attempts']==1


def test_personal_delivery_skips_old_notices_and_rechecks_current_subscription(delivery_model):
    worker,alerts,selected,sent = delivery_model
    selected[0] = replace(selected[0],notify_webhook_url='')
    now = time.time()+1
    target = {'identity':'owner','since':now,'choices':{'notify_ntfy_enabled':True,'notify_ntfy_url':'http://127.0.0.1:1',
              'notify_ntfy_topic':'private','notify_operational':False},'notices':{'items':[
                  dict(id='old',at=now-10,title='Old notice',category='Security',href='/account/logs'),
                  dict(id='new',at=now+1,title='New notice',category='Security',href='/account/logs')]}}
    worker.plan([target],now=now+2)
    async def disabled(owner):
        return {}
    worker.recipient_lookup = disabled
    asyncio.run(worker.process([target],now=now+2))
    assert not sent and worker.status('account:owner')[0]['state']=='skipped'
    assert len(worker.status('account:owner'))==1
    # A new notice waits through an unavailable authority without consuming a try.
    target['notices']['items'] = [dict(id='later',at=now+3,title='Later notice',category='Security',href='/account/logs')]
    worker.plan([target],now=now+4)
    async def unavailable(owner):
        return None
    worker.recipient_lookup = unavailable
    asyncio.run(worker.process([target],now=now+4))
    assert worker.status('account:owner')[0]['attempts']==0
    async def fresh(owner):
        return {'choices':target['choices'],'since':now}
    worker.recipient_lookup = fresh
    asyncio.run(worker.process([target],now=now+65))
    assert len(sent)==1 and sent[0][3]=='account:owner' and sent[0][1]['title']=='Later notice'


def fleet_report():
    return {'poll_seconds':10,'machines':[{'name':'node','status_live':True,'age':0,'accepting_work':True}],
            'hosts':[{'fingerprint':'host-id','hostname':'node','age':0,'telemetry':{
                'memory':{'used':90,'total':100},'disks':[{'path':'/','free':5,'total':100}],
                'cards':[{'card':0,'memory_used':95,'memory_total':100}]}}]}


def test_resource_thresholds_apply_live_and_missing_evidence_cannot_resolve(tmp_path):
    alerts = Alerts(tmp_path/'alerts.db')
    try:
        settings = Settings(key='test',alert_gpu_memory_percent=90,alert_host_ram_percent=80)
        report = fleet_report()
        readings = alerts.resource_conditions(report,settings,1000)
        assert all(readings[source][0]['active'] for source in ('Disk space','GPU memory','Host RAM'))
        for source,rows in readings.items():
            alerts.observe(source,rows,1000,missing_recovers=False)
        report['hosts'][0]['age'] = 100
        missing = alerts.resource_conditions(report,settings,1001)
        for source in ('Disk space','GPU memory','Host RAM'):
            assert missing[source][0]['active'] is None
            alerts.observe(source,missing[source],1001,missing_recovers=False)
        assert all(row['state']=='unknown' for row in alerts.rows(open_only=True))
        disabled = replace(settings,alert_disk_free_percent=0,alert_gpu_memory_percent=0,alert_host_ram_percent=0)
        for source,rows in alerts.resource_conditions(None,disabled,1002).items():
            alerts.observe(source,rows,1002,missing_recovers=False)
        assert not alerts.rows(open_only=True)
    finally:
        alerts.db.close()


def test_node_silence_clock_persists_and_a_new_threshold_applies_without_restart(tmp_path):
    path = tmp_path/'alerts.db'
    alerts = Alerts(path)
    settings = Settings(key='test')
    report = {'machines':[{'name':'node','status_live':False,'age':601,'accepting_work':True}],'hosts':[],'poll_seconds':10}
    assert alerts.resource_conditions(report,settings,1000)['Node silence'][0]['active'] is True
    alerts.db.close()
    alerts = Alerts(path)
    try:
        report['machines'][0]['age'] = None
        assert alerts.resource_conditions(report,settings,1005)['Node silence'][0]['active'] is True
        assert alerts.resource_conditions(report,replace(settings,alert_node_offline_minutes=20),1005)['Node silence'][0]['active'] is False
        report['machines'][0].update(status_live=True,age=0)
        assert alerts.resource_conditions(report,settings,1006)['Node silence'][0]['active'] is False
        assert not alerts.db.execute('SELECT 1 FROM alert_node_silence').fetchone()
    finally:
        alerts.db.close()


def test_system_channel_settings_and_write_only_actions_are_live(desk,receiver):
    server,switch,_ = desk
    url,records = receiver
    sign_in(server,'jordan')
    assert server.get('/settings/alerts').status_code == 200
    saved = server.post('/settings/weightroom',data={'return_to':'/settings/alerts','value-notify_webhook_url':url+'/hook',
                        'value-notify_ntfy_url':url,'value-notify_ntfy_topic':'system'})
    assert saved.status_code == 200,saved.text
    for name,value in [('notify_webhook_secret','fake-hidden-hmac'),('notify_ntfy_token','fake-hidden-ntfy')]:
        answer = server.post('/settings/alerts/credentials/'+name,data={'credential':value,'operation':'save'})
        assert answer.status_code == 200 and value not in answer.text
    assert server.post('/settings/alerts/test/webhook').status_code == 200
    assert server.post('/settings/alerts/test/ntfy').status_code == 200
    assert [record[0] for record in records] == ['/hook','/']
    assert records[-1][1]['Authorization']=='Bearer fake-hidden-ntfy'
    raw = switch.front.state.console_settings.changes.db.execute('SELECT told FROM settings_seen WHERE program=?',('weightroom',)).fetchone()[0]
    assert 'fake-hidden' not in raw
    assert 'fake-hidden' not in server.get('/settings/history').text
    bad = server.post('/settings/alerts/credentials/notify_ntfy_token',data={'credential':'fake-invalid\nsecret','operation':'save'})
    assert bad.status_code == 400 and 'fake-invalid' not in bad.text
    assert len(records)==2
    assert server.post('/settings/weightroom',data={'return_to':'/settings/alerts','value-notify_ntfy_url':'','value-notify_ntfy_topic':''}).status_code == 200
    assert server.post('/settings/alerts/test/ntfy').status_code == 400 and len(records)==2


def test_personal_channels_are_owner_scoped_and_operational_subscription_is_authorized(desk,receiver):
    server,switch,users = desk
    url,records = receiver
    sign_in(server,'pat')
    owner = server.cookies['weightroom']
    assert server.post('/settings/alerts/test/ntfy').status_code == 403
    assert server.post('/account/preferences/notifications',data={'value-notify_ntfy_enabled':'true','value-notify_ntfy_url':url,
                       'value-notify_ntfy_topic':'personal'}).status_code == 200
    saved = server.post('/account/notifications/credentials/notify_ntfy_token',data={'credential':'fake-personal-private','operation':'save'})
    assert saved.status_code == 200 and 'fake-personal-private' not in saved.text
    assert server.post('/account/notifications/test/ntfy').status_code == 200
    assert records[-1][1]['Authorization']=='Bearer fake-personal-private'
    assert account_api(server,switch,owner,'POST','/loadcoach/me',json={'notify_operational':True}).status_code == 403
    assert account_api(server,switch,owner,'GET','/turnstile/notification-feed').status_code == 403
    internal = users.new_key('panel')
    feed = account_api(server,switch,internal,'GET','/turnstile/notification-feed')
    assert feed.status_code == 200 and len(feed.json()['targets'])==1
    assert feed.json()['targets'][0]['identity']==users.identity('pat') and 'fake-personal-private' not in feed.text
    assert server.post('/account/keys',data={'label':'Notice test'}).status_code == 200
    server.portal.call(server.app.state.collect_alerts,internal)
    targets = account_api(server,switch,internal,'GET','/turnstile/notification-feed').json()['targets']
    server.portal.call(server.app.state.notifications.process,targets)
    assert len(records)>=2 and all(json.loads(record[2])['topic']=='personal' for record in records)
    # A different account has neither the token nor its delivery history.
    sign_in(server,'jordan')
    assert 'fake-personal-private' not in server.get('/account/privacy').text
    assert server.app.state.notifications.keys.read('notify_ntfy_token',users.identity('jordan'))==''


def test_operational_allowance_percent_reads_current_catalogue_policy(desk):
    from turnstile.allowances import Allowances
    server,switch,users = desk
    sign_in(server,'jordan')
    log = switch.front.state.log
    Allowances(users,log).save(users.by_name('jordan'),'user',users.identity('pat'),10)
    log.add('request','done','pat',cost=6,account_id=users.identity('pat'),group='plain')
    internal = users.new_key('panel')
    for percent,active in ((50,True),(80,False)):
        switch.front.state.console_settings.changes.put('weightroom','alert_allowance_percent',percent,'test')
        response = account_api(server,switch,internal,'GET','/turnstile/notification-feed')
        assert response.status_code == 200,response.text
        assert response.json()['allowance_conditions'][0]['active'] is active


def test_all_notification_secret_entries_are_filtered_from_reports_and_exports(desk):
    server,switch,_ = desk
    sign_in(server,'jordan')
    for name in ('notify_webhook_secret','notify_ntfy_token','notify_smtp_password'):
        value = 'fake-never-render-'+name
        assert server.post('/settings/alerts/credentials/'+name,data={'credential':value,'operation':'save'}).status_code == 200
        assert value not in server.get('/settings/alerts').text
    exported = server.get('/settings/history?export=json')
    assert exported.status_code == 200,exported.text
    assert 'fake-never-render' not in exported.text
    for (raw,) in switch.front.state.console_settings.changes.db.execute('SELECT told FROM settings_seen'):
        assert 'fake-never-render' not in raw



@pytest.mark.parametrize('width',[1440,390])
def test_notification_forms_fit_and_search_credentials_without_retaining_values(desk,width):
    from test_console_browser import browser_context,playwright
    server,_,_ = desk
    sign_in(server,'jordan')
    with playwright.sync_playwright() as engine:
        browser,page = browser_context(engine,server)
        page.set_viewport_size({'width':width,'height':1000})
        page.goto('http://console.test/settings/alerts')
        search = page.get_by_label('Find a setting',exact=True)
        search.fill('SMTP host')
        host = page.locator('[name="value-notify_smtp_host"]')
        playwright.expect(host).to_be_visible()
        page.evaluate("document.querySelector('[name=value-notify_smtp_host]').closest('.settings-row').replaceWith(document.querySelector('[name=value-notify_smtp_host]').closest('.settings-row').cloneNode(true))")
        playwright.expect(host).to_be_visible()
        search.fill('ntfy bearer token')
        token = page.locator('#credential-weightroom-notify_ntfy_token')
        playwright.expect(token).to_be_visible()
        token.fill('fake-browser-only-private')
        page.get_by_label('Changed only',exact=True).check()
        playwright.expect(token).to_be_visible()
        assert page.evaluate('JSON.stringify(sessionStorage)').find('fake-browser-only-private') == -1
        page.locator('form').filter(has=token).get_by_role('button',name='Save credential',exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Credential saved')
        playwright.expect(token).to_have_value('')
        assert 'fake-browser-only-private' not in page.content()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # The intercepted native POST follows its 303 inside TestClient; reopen the canonical URL.
        page.goto('http://console.test/settings/alerts')
        page.get_by_role('button',name='Clear filters',exact=True).click()
        page.get_by_role('button',name='Send test email',exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Set an SMTP server')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto('http://console.test/account/privacy')
        playwright.expect(page.locator('[name="value-notify_ntfy_url"]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


def test_personal_notification_preferences_save_without_javascript(desk):
    from test_console_browser import playwright
    from front_helpers import serve_browser_request
    server,_,users = desk
    sign_in(server,'pat')
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(java_script_enabled=False,viewport={'width':390,'height':1000})
        page.route('http://console.test/**',lambda route:serve_browser_request(server,'http://console.test',route))
        page.goto('http://console.test/account/privacy')
        page.locator('[name="value-notify_ntfy_url"]').fill('http://127.0.0.1:1')
        page.locator('[name="value-notify_ntfy_topic"]').fill('private-test')
        page.get_by_role('button',name='Save delivery preferences',exact=True).click()
        playwright.expect(page.locator('.said-box')).to_contain_text('Preferences saved')
        assert users.choices('pat')['notify_ntfy_topic']=='private-test'
        assert not users.choices('pat')['notify_ntfy_enabled']
        browser.close()


def test_postgresql_notification_queue_and_opt_in_metadata(postgres,tmp_path):
    from loadcoach.groups import Group
    from loadcoach.users import Users
    users = Users(postgres,{'plain':Group()})
    users.add('owner','plain')
    users.choose('owner',{'notify_email':'owner@example.test','notify_email_enabled':True})
    assert users.db.execute('SELECT notification_started FROM users WHERE name=?',('owner',)).fetchone()[0]>0
    alerts = Alerts(postgres)
    selected = Settings(key='test',notify_webhook_url='http://127.0.0.1:1/hook')
    calls = []
    class Capture:
        async def send(self,*args):
            calls.append(args)
    worker = Notifications(alerts,lambda:selected,NotificationKeys(tmp_path/'keys'),Capture())
    now = time.time()+1
    alerts.observe('Disk space',[reading(True)],now)
    worker.plan(now=now)
    asyncio.run(worker.process(now=now))
    assert len(calls)==1 and worker.status()[0]['state']=='sent'
    alerts.db.close();users.db.close()



def test_personal_legacy_identity_cannot_overwrite_the_system_channel(desk,receiver):
    server,_,users = desk
    url,_ = receiver
    with users.db:
        users.db.execute('UPDATE users SET identity=? WHERE name=?',('system','pat'))
    keys = server.app.state.notifications.keys
    keys.save('notify_ntfy_token','fake-system-credential')
    sign_in(server,'pat')
    assert server.post('/account/notifications/credentials/notify_ntfy_token',data={'credential':'fake-own-credential','operation':'save'}).status_code==200
    assert keys.read('notify_ntfy_token')=='fake-system-credential'
    assert keys.read('notify_ntfy_token','account:system')=='fake-own-credential'



def test_pending_delivery_rechecks_live_delay_and_repeat_policies(delivery_model):
    worker,alerts,selected,sent = delivery_model
    now = time.time()+1
    alerts.observe('Disk space',[reading(True)],now)
    worker.plan(now=now)
    selected[0] = replace(selected[0],notify_min_state='unacknowledged',notify_after_minutes=15)
    asyncio.run(worker.process(now=now+1))
    assert not sent and worker.status()[0]['attempts']==0
    selected[0] = replace(selected[0],notify_after_minutes=1)
    asyncio.run(worker.process(now=now+61))
    assert len(sent)==1
    selected[0] = replace(selected[0],notify_repeat_minutes=1)
    worker.plan(now=now+122)
    selected[0] = replace(selected[0],notify_repeat_minutes=0)
    asyncio.run(worker.process(now=now+122))
    assert len(sent)==1 and worker.status()[0]['state']=='skipped'



def test_monitor_policy_uses_machine_file_fallback_after_a_live_reset(tmp_path):
    from starlette.testclient import TestClient
    from loadcoach.groups import Group
    from loadcoach.users import Users
    from turnstile.app import Settings as ControlSettings,build_app
    file = tmp_path/'weightroom.toml'
    file.write_text('alert_allowance_percent = 40\n')
    users = Users(':memory:',{'mirror':Group(can=frozenset({'mirror-users'}))})
    users.add('panel','mirror')
    app = build_app(ControlSettings(path=str(file),data=str(tmp_path)),users=users)
    with TestClient(app):
        assert app.state.live_settings('weightroom','alert_allowance_percent',0)==40
        changes = app.state.console_settings.changes
        changes.put('weightroom','alert_allowance_percent',60,'test')
        assert app.state.live_settings('weightroom','alert_allowance_percent',0)==60
        changes.take_away('weightroom','alert_allowance_percent')
        assert app.state.live_settings('weightroom','alert_allowance_percent',0)==40
    users.db.close()



@pytest.mark.parametrize('name,value',[('notify_ntfy_enabled',1),('notify_ntfy_url',[]),('notify_ntfy_topic',[]),
                                       ('notify_email_enabled',1),('notify_email',[]),('notify_operational',1)])
def test_personal_field_types_use_the_catalogue_and_cannot_be_service_overrides(desk,name,value):
    from loadcoach.setting_catalog import validate,changeable
    server,switch,users = desk
    sign_in(server,'pat')
    key = server.cookies['weightroom']
    before = users.choices('pat')
    with pytest.raises(ValueError) as problem:
        validate('account',name,value)
    failed = account_api(server,switch,key,'POST','/loadcoach/me',json={name:value})
    assert failed.status_code==400 and failed.json()['error']['message']==str(problem.value)
    assert users.choices('pat')==before
    assert 'account' not in changeable()
