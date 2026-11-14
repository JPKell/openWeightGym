"""Durable daily chat expiry and the live built-in control node backup policy."""
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

import pytest

from benchmarks.store import Benchmarks
from db import migrate
from db.maintenance import Gate
from db.schemas import CHAT
from loadcoach.setting_catalog import validate
from weightroom.chat_store import Conversations
from weightroom.schedules import BUILTIN_BACKUP, Schedules, ScheduleWorker
from test_weightroom import desk, sign_in  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_console_browser import browser_context, playwright

NOW = int(time.time())
DAY = 86400


@pytest.mark.parametrize('name,wrong', [
    ('chat_keep_days',-1), ('chat_keep_days',36501), ('chat_keep_days',True),
    ('backup_every','monthly'), ('backup_time','3:00'), ('backup_time','24:00'),
    ('backup_time','03:60'), ('backup_time','03:00\n'), ('backup_time',30),
])
def test_retention_ranges_are_catalogue_contracts(name,wrong):
    with pytest.raises(ValueError): validate('weightroom',name,wrong)


def age(chats,number,at=NOW-10*DAY):
    with chats.db:
        chats.db.execute('UPDATE conversations SET updated=?,last_activity=? WHERE id=?',(at,at,number))


def expiry_contract(source):
    chats=Conversations(source)
    old=chats.start('one','old'); chats.say(old,'user','old message'); age(chats,old)
    opened=chats.start('one','opened'); age(chats,opened); chats.touch(opened,'one',NOW)
    active=chats.start('two','active',kind='tools'); chats.answering(active,0,'two','model'); age(chats,active)
    recent=chats.start('two','recent'); age(chats,recent,NOW-DAY)
    before=chats.db.execute('SELECT updated FROM conversations WHERE id=?',(opened,)).fetchone()[0]
    assert chats.prune(0,NOW)==0 and chats.retention_status(0)['last_sweep'] is None
    assert not chats.touch(old,'foreign',NOW)
    assert chats.prune(5,NOW)==1
    assert chats.get(old,'one') is None and chats.get(opened,'one') and chats.get(active,'two')
    assert chats.db.execute('SELECT updated FROM conversations WHERE id=?',(opened,)).fetchone()[0]==before
    assert chats.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0
    # Neither a late write nor a late turn can create an orphan after expiry.
    chats.say(old,'assistant','late'); chats.answering(old,0,'one','model')
    assert not chats.db.execute('SELECT * FROM answering WHERE conversation=?',(old,)).fetchall()
    assert not chats.db.execute('SELECT * FROM messages WHERE conversation=?',(old,)).fetchall()
    chats.answered(active,0)
    assert chats.prune(5,NOW+10)==0
    chats.say(recent,'user','new activity')
    assert chats.prune(5,NOW+DAY)==1  # the completed old answer expires; opened/new-message chats remain
    assert chats.get(recent,'two') and chats.get(opened,'one')
    assert chats.retention_status(5)['last_sweep']==NOW+DAY
    chats.db.close()
    restarted=Conversations(source)
    assert restarted.prune(5,NOW+DAY+10)==0
    restarted.db.close()


def test_expiry_daily_claim_active_answers_openings_and_messages(tmp_path):
    expiry_contract(tmp_path/'chat.db')


def test_existing_chat_schema_preserves_history_and_seeds_activity(tmp_path):
    from db import database
    source=database(tmp_path/'old.db')
    with source.connection() as db:
        migrate(source,db,'weightroom.chat',CHAT[:-1])
        with db:
            db.execute('INSERT INTO conversations(user,title,system,thinking,updated) VALUES(?,?,?,?,?)',('one','old','',1,12345))
    chats=Conversations(source)
    assert chats.db.execute('SELECT updated,last_activity FROM conversations').fetchone()==(12345,12345)
    chats.db.close()


def test_builtin_policy_off_default_and_live_recalculation(tmp_path):
    store=Schedules(tmp_path/'schedules.db')
    store.sync_backup_policy('off','03:00','UTC',10,NOW)
    assert store.rows()==[]
    store.sync_backup_policy('daily','03:00','UTC',10,NOW)
    row=store.rows()[0]; assert row['builtin'] and row['grant_token']=='' and row['spec']['nodes']==['front']
    due=row['next_at']; store.claim(due)
    store.sync_backup_policy('weekly','04:12','America/New_York',3,due+1)
    row=store.rows()[0]
    when=datetime.fromtimestamp(row['next_at'],ZoneInfo('America/New_York'))
    assert when.weekday()==6 and (when.hour,when.minute)==(4,12)
    assert row['spec']['retention']==3 and store.occurrences()[0]['state']=='cancelled'
    for edit in [lambda:store.enable(BUILTIN_BACKUP,False),lambda:store.delete(BUILTIN_BACKUP),
                 lambda:store.update(BUILTIN_BACKUP,'renamed',row['spec'],'token'),
                 lambda:store.create(BUILTIN_BACKUP,'renamed','one','one',row['spec'],'token')]:
        with pytest.raises(ValueError,match='Retention'):edit()
    store.sync_backup_policy('off','04:12','America/New_York',3,due+1)
    assert not store.rows()[0]['enabled'] and store.occurrences()
    store.db.close()


def test_builtin_uses_verified_local_writer_retries_same_id_without_remote_grant(tmp_path):
    async def exercise():
        policy=['daily','03:00','UTC',10]; store=Schedules(tmp_path/'schedules.db')
        benchmarks=Benchmarks(tmp_path/'benchmarks.db'); remote=AsyncMock(); writer=AsyncMock(side_effect=[OSError(),None])
        worker=ScheduleWorker(store,remote,benchmarks,writer,Gate(),asyncio.Event(),lambda:tuple(policy))
        await worker.tick(NOW); due=store.rows()[0]['next_at']
        await worker.tick(due); assert store.occurrences()[0]['state']=='dispatching'
        await worker.tick(due+1); assert store.occurrences()[0]['state']=='completed'
        assert writer.await_args_list[0]==writer.await_args_list[1] and writer.await_args.args[1]==10
        remote.post.assert_not_awaited()
        assert store.occurrences()[0]['backup_nodes'][0]['state']=='verified'
        policy[:]=['weekly','05:40','UTC',2]
        await worker.tick(due+2)
        assert store.rows()[0]['spec']['time']=='05:40' and store.rows()[0]['spec']['retention']==2
        policy[0]='off'; await worker.tick(due+3); assert not store.rows()[0]['enabled']
        store.db.close(); benchmarks.db.close()
    asyncio.run(exercise())


def test_policy_change_does_not_interrupt_a_submitted_backup(tmp_path):
    async def exercise():
        policy=['daily','03:00','UTC',10]; store=Schedules(tmp_path/'schedules.db')
        benchmarks=Benchmarks(tmp_path/'benchmarks.db'); began=asyncio.Event();finish=asyncio.Event()
        async def writer(identifier,retention):
            began.set();await finish.wait();assert retention==10
        worker=ScheduleWorker(store,AsyncMock(),benchmarks,writer,Gate(),asyncio.Event(),lambda:tuple(policy))
        await worker.tick(NOW);due=store.rows()[0]['next_at']
        writing=asyncio.create_task(worker.tick(due));await began.wait()
        policy[0]='off';finish.set();await writing;await worker.tick(due+1)
        assert store.occurrences()[0]['state']=='completed' and not store.rows()[0]['enabled']
        store.db.close();benchmarks.db.close()
    asyncio.run(exercise())


def test_chat_cleanup_respects_existing_maintenance_barrier(desk,tmp_path):
    browser,wire,users=desk; sign_in(browser,'jordan')
    conversations=browser.app.state.conversations
    gate=conversations.state.maintenance;gate.path=tmp_path/'maintenance-marker.json'
    gate.path.write_text('{"token":"test-maintenance"}')
    assert browser.portal.call(conversations.state.retention_sweep)==0
    assert gate.active==0
    gate.path.unlink()


def wait_for(check):
    until=time.monotonic()+4
    while time.monotonic()<until:
        if check():return
        time.sleep(.02)
    assert check()


def test_retention_and_backup_policy_take_effect_in_running_process(desk):
    browser,wire,users=desk; sign_in(browser,'jordan')
    chat=browser.app.state.conversations.state.chats
    owner=users.identity('jordan'); number=chat.start(owner,'expiry test'); age(chat,number,time.time()-10*DAY)
    assert browser.post('/settings/weightroom',data={'return_to':'/settings/retention','value-chat_keep_days':'5',
        'value-backup_every':'"weekly"','value-backup_time':'04:12'}).status_code==200
    store=browser.app.state.schedules
    wait_for(lambda: bool(store.rows()) and store.rows()[0]['spec']['time']=='04:12')
    assert store.rows()[0]['spec']['weekdays']==[6]
    browser.portal.call(browser.app.state.conversations.state.retention_sweep)
    assert chat.get(number,owner) is None
    shown=browser.get('/schedules?view='+BUILTIN_BACKUP)
    assert shown.status_code==200 and 'Backup policy' in shown.text and 'Retention and privacy' in shown.text
    assert 'Control node' in shown.text and 'name="do" value="pause"' not in shown.text
    action=browser.post('/schedules/'+BUILTIN_BACKUP,data={'do':'pause'})
    assert action.status_code==200 and action.url.path=='/settings/retention' and store.rows()[0]['enabled']
    browser.post('/settings/weightroom',data={'return_to':'/settings/retention','value-chat_keep_days':'0','value-backup_every':'"off"'})
    wait_for(lambda:not store.rows()[0]['enabled'])
    fresh=chat.start(owner,'kept'); age(chat,fresh,time.time()-10*DAY)
    browser.portal.call(browser.app.state.conversations.state.retention_sweep)
    assert chat.get(fresh,owner)


def test_authorized_opening_renews_activity_but_polling_and_other_accounts_do_not(desk):
    browser,wire,users=desk; sign_in(browser,'jordan'); chats=browser.app.state.conversations.state.chats
    owner=users.identity('jordan'); number=chats.start(owner,'opened chat'); age(chats,number)
    assert browser.get('/chat/'+str(number)+'?part=main').status_code==200
    assert chats.db.execute('SELECT last_activity FROM conversations WHERE id=?',(number,)).fetchone()[0]==NOW-10*DAY
    sign_in(browser,'pat'); assert browser.get('/chat/'+str(number)).status_code==404
    assert chats.db.execute('SELECT last_activity FROM conversations WHERE id=?',(number,)).fetchone()[0]==NOW-10*DAY
    sign_in(browser,'jordan'); assert browser.get('/chat/'+str(number)).status_code==200
    assert chats.db.execute('SELECT last_activity FROM conversations WHERE id=?',(number,)).fetchone()[0]>time.time()-10


@pytest.mark.parametrize('width',[390,1440])
def test_retention_controls_error_search_and_builtin_inspector_fit(desk,width):
    browser,wire,users=desk;sign_in(browser,'jordan')
    with playwright.sync_playwright() as engine:
        chromium,page=browser_context(engine,browser)
        try:
            page.set_viewport_size({'width':width,'height':1000});page.goto('http://console.test/settings/retention')
            playwright.expect(page.locator('#setting-weightroom-chat_keep_days')).to_have_value('0')
            page.locator('#settings-search').fill('not-a-retention-setting')
            playwright.expect(page.locator('[data-settings-no-match]')).to_be_visible()
            page.locator('#settings-search').fill('Automatic backup time')
            page.locator('#setting-weightroom-backup_time').fill('25:00')
            section=page.locator('#setting-weightroom-backup_time').locator('xpath=ancestor::div[@data-settings-section]')
            section.locator('[data-save-settings]').click()
            playwright.expect(section.locator('[data-settings-status]')).to_contain_text('HH:MM')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            browser.app.state.schedules.sync_backup_policy('weekly','03:00','UTC',10)
            page.goto('http://console.test/schedules?view='+BUILTIN_BACKUP)
            playwright.expect(page.get_by_role('link',name='Backup policy',exact=True)).to_be_visible()
            assert page.locator('#schedule-form input[name=name]').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        finally:chromium.close()


def test_postgresql_daily_expiry_and_builtin_policy(postgres):
    expiry_contract(postgres)
    store=Schedules(postgres);store.sync_backup_policy('daily','03:00','UTC',3,NOW)
    due=store.rows()[0]['next_at'];store.claim(due);store.claim(due+1)
    assert len(store.occurrences())==1
    store.sync_backup_policy('off','03:00','UTC',3,due+2)
    assert store.occurrences()[0]['state']=='cancelled'
    store.db.close()
