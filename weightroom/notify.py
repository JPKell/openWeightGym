"""Write-only notification keys and explicit webhook, ntfy and SMTP transports."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime,timezone
from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl
import time
import uuid
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import httpx

from loadcoach.setting_catalog import get
from voice.telephone_keys import atomic_secret


def account_owner(identity):
    """Account namespaces never collide with the system channel, even for legacy identities."""
    return 'account:'+identity


class NotificationKeys:
    """Read credentials only at use; expose existence and timestamps separately."""

    def __init__(self,folder):
        """Bind live delivery settings, private keys, HTTP transport and SMTP context."""
        self.folder = Path(folder)

    def path(self,name,owner='system'):
        """Validate the credential field and derive an owner-scoped private filename."""
        program = 'weightroom' if owner == 'system' else 'account'
        entry = get(program,name)
        if not entry or entry.kind != 'secret' or not name.startswith('notify_'):
            raise ValueError('Choose a notification credential.')
        scope = 'system' if owner == 'system' else hashlib.sha256(owner.encode()).hexdigest()
        return self.folder/('notifications-'+scope+'-'+name+'.key')

    def read(self,name,owner='system'):
        """Read the selected private notification credential, or empty text if absent."""
        try:
            return self.path(name,owner).read_text().removesuffix('\n')
        except FileNotFoundError:
            return ''

    def save(self,name,value,owner='system'):
        """Validate and atomically save the selected owner's nonempty credential."""
        path = self.path(name,owner)
        get('weightroom' if owner=='system' else 'account',name).check(value)
        if value:
            atomic_secret(path,value)

    def remove(self,name,owner='system'):
        """Delete only the selected owner's notification credential."""
        self.path(name,owner).unlink(missing_ok=True)

    def status(self,name,owner='system'):
        """Read bounded notification delivery outcomes or credential presence without secrets."""
        try:
            stamp = self.path(name,owner).stat().st_mtime
            return {'saved':True,'changed':datetime.fromtimestamp(stamp,timezone.utc).isoformat()}
        except FileNotFoundError:
            return {'saved':False,'changed':None}


def payload(episode,site_url='',state=None):
    """Only episode metadata travels to an external channel; no request content."""
    link = '/alerts?scope=all&selected='+episode['id']
    return {'episode_id':episode['id'],'condition':episode['condition'],'source':episode['source'],
            'title':episode['title'],'state':state or ('resolved' if episode.get('resolved_at') else 'opened'),
            'first_at':episode['first_at'],'last_at':episode['last_at'],'resolved_at':episode.get('resolved_at'),
            'link':urljoin(site_url.rstrip('/')+'/',link.lstrip('/')) if site_url else link}


class DeliveryError(Exception):
    """A sanitized failure; never contain an endpoint, credential or provider response."""


class Sender:
    """One bounded delivery attempt. The durable worker schedules retries."""

    def __init__(self,settings,keys,client=None,smtp_context=None):
        """Bind durable notification planning to alerts, live settings and its sender."""
        self.settings,self.keys,self.client,self.smtp_context = settings,keys,client,smtp_context

    async def http(self,url,**kwargs):
        """Send a bounded notification request without buffering the provider's body."""
        async def post(client):
            # Provider response bodies are never buffered, logged or retained.
            """Read only the provider's response status while closing its streamed body."""
            async with client.stream('POST',url,timeout=10,follow_redirects=False,**kwargs) as answer:
                return answer.status_code
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
                    status = await post(client)
            else:
                status = await post(self.client)
            if not 200 <= status < 300:
                raise DeliveryError(f'HTTP delivery returned {status}.')
        except httpx.HTTPError:
            raise DeliveryError('HTTP delivery could not reach the channel.') from None

    async def send(self,channel,message,recipient=None,owner='system'):
        """Personal channels receive their owner's destination and key, never a system token."""
        selected = self.settings()
        if channel == 'webhook' and owner == 'system':
            secret = self.keys.read('notify_webhook_secret')
            if not selected.notify_webhook_url or not secret:
                raise DeliveryError('Set a webhook URL and signing secret before sending.')
            body = json.dumps(message,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()
            signature = hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
            await self.http(selected.notify_webhook_url,content=body,
                            headers={'Content-Type':'application/json','X-Suite-Signature':'sha256='+signature})
        elif channel == 'ntfy':
            url = recipient['notify_ntfy_url'] if recipient else selected.notify_ntfy_url
            topic = recipient['notify_ntfy_topic'] if recipient else selected.notify_ntfy_topic
            if not url or not topic:
                raise DeliveryError('Set an ntfy server URL and topic before sending.')
            token = self.keys.read('notify_ntfy_token',owner)
            body = {'topic':topic,'title':message['title'][:200],'message':message['state']+' · '+message['source']+'\n'+message['link']}
            if message['link'].startswith(('http://','https://')):
                body['click'] = message['link']
            await self.http(url.rstrip('/')+'/',json=body,headers={'Authorization':'Bearer '+token} if token else {})
        elif channel == 'email':
            to = recipient['notify_email'] if recipient else selected.notify_smtp_to
            if not selected.notify_smtp_host or not selected.notify_smtp_from or not to:
                raise DeliveryError('Set an SMTP server, sender and recipient before sending.')
            try:
                await asyncio.wait_for(asyncio.to_thread(self.mail,message,to,selected,self.keys.read('notify_smtp_password')),30)
            except (OSError,smtplib.SMTPException,TimeoutError,ValueError):
                raise DeliveryError('SMTP delivery failed; check the server, TLS and credentials.') from None
        else:
            raise DeliveryError('Choose webhook, ntfy or email.')

    def mail(self,message,to,selected,password):
        """SMTP credentials remain inside this bounded worker thread."""
        host,port = selected.notify_smtp_host,selected.notify_smtp_port
        security = selected.notify_smtp_security
        context = self.smtp_context or ssl.create_default_context()
        tls = security == 'tls' or security == 'auto' and port == 465
        connection = smtplib.SMTP_SSL(host,port,timeout=10,context=context) if tls else smtplib.SMTP(host,port,timeout=10)
        with connection as smtp:
            smtp.ehlo()
            if not tls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if username := selected.notify_smtp_username:
                smtp.login(username,password)
            mail = EmailMessage()
            mail['From'],mail['To'],mail['Subject'] = selected.notify_smtp_from,to,' '.join(message['title'].splitlines())[:200]
            mail.set_content(message['state']+' · '+message['source']+'\n'+message['link'])
            refused = smtp.send_message(mail)
            if refused:
                raise DeliveryError('SMTP server refused a recipient.')



def quiet_now(settings,now):
    """Quiet hours are wall-clock intervals in the configured control node zone."""
    if not settings.notify_quiet_hours:
        return False
    local = datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo(settings.timezone) if settings.timezone else None)
    start,end = settings.notify_quiet_hours.split('-')
    clock = local.strftime('%H:%M')
    return start <= clock < end if start < end else clock >= start or clock < end


def channels(settings,recipient=None):
    """List only notification channels enabled for the selected system or personal recipient."""
    if recipient is not None:
        return [name for name,enabled in (('ntfy',recipient.get('notify_ntfy_enabled')),('email',recipient.get('notify_email_enabled'))) if enabled]
    return [name for name,enabled in (('webhook',settings.notify_webhook_url),
                                     ('ntfy',settings.notify_ntfy_url or settings.notify_ntfy_topic),
                                     ('email',settings.notify_smtp_host and settings.notify_smtp_to)) if enabled]


class Notifications:
    """Durable per-episode deliveries; no provider URLs, responses or credentials in the queue."""

    def __init__(self,alerts,settings,keys,sender=None):
        """Bind notification delivery to its private keys, settings and existing store."""
        self.alerts,self.db,self.settings,self.keys = alerts,alerts.db,settings,keys
        self.sender = sender or Sender(settings,keys)
        self.lock = asyncio.Lock()
        self.recipient_lookup = None
        self.system_since = time.time()
        self.cursor('system',self.system_configuration(),self.system_since)

    def system_configuration(self):
        """Read the system delivery fields used for current notification planning."""
        selected = self.settings()
        return {name:getattr(selected,name) for name in ('notify_webhook_url','notify_ntfy_url','notify_ntfy_topic',
                'notify_smtp_host','notify_smtp_port','notify_smtp_security','notify_smtp_from','notify_smtp_to','notify_smtp_username')}


    def cursor(self,owner,recipient,now,began=None):
        """Track recipient configuration revisions and notice-planning instants."""
        configuration = hashlib.sha256(json.dumps({name:value for name,value in recipient.items() if name.startswith(('notify_','alert_'))},sort_keys=True).encode()).hexdigest()
        with self.db:
            row = self.db.execute('SELECT since,last_planned,configuration FROM notification_cursors WHERE owner=?',(owner,)).fetchone()
            if not row or row[2] != configuration:
                began = min(now,max(0,began)) if began is not None and (not row or began>row[1]) else now
                self.db.execute('INSERT INTO notification_cursors(owner,since,last_planned,configuration) VALUES(?,?,?,?) '
                                'ON CONFLICT(owner) DO UPDATE SET since=excluded.since,last_planned=excluded.last_planned,configuration=excluded.configuration',
                                (owner,began,began,configuration))
                return began,began
        return row[0],row[1]

    def enqueue(self,episode,owner,channel,phase,message,now):
        """Deduplicate a notification by episode, owner, channel and lifecycle phase."""
        identifier = hashlib.sha256(json.dumps([episode,owner,channel,phase],separators=(',',':')).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO notification_deliveries(id,episode,owner,channel,phase,payload,created,next_at) '
                            'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                            (identifier,episode,owner,channel,phase,json.dumps(message),now,now))

    def plan(self,targets=(),now=None):
        """Snapshot transitions after reconciliation; send only newly opted-in personal notices."""
        now = time.time() if now is None else now
        settings = self.settings()
        since,last = self.cursor('system',self.system_configuration(),now)
        recipients = [('system',None,since,last)]
        for target in targets:
            owner,recipient = account_owner(target['identity']),target['choices']
            since,last = self.cursor(owner,recipient,now,target.get('since'))
            recipients.append((owner,recipient,since,last))
            for item in target['notices']['items']:
                if not item.get('at') or item['at'] < since:
                    continue
                message = {'episode_id':item['id'],'condition':'personal:'+item['id'],'source':item['category'],
                           'title':item['title'],'state':'notice','first_at':item['at'],'last_at':item['at'],
                           'resolved_at':None,'link':urljoin(settings.notify_site_url.rstrip('/')+'/',item['href'].lstrip('/')) if settings.notify_site_url else item['href']}
                for channel in channels(settings,recipient):
                    self.enqueue(item['id'],owner,channel,'notice',message,now)
        episodes = [*self.alerts.rows(open_only=True),*self.alerts.resolved_since(min(row[3] for row in recipients))]
        for owner,recipient,since,last in recipients:
            if recipient is not None and not recipient.get('notify_operational'):
                continue
            for episode in episodes:
                resolved = episode['resolved_at'] is not None
                if resolved and episode['resolved_at'] < last or owner != 'system' and episode['first_at'] < since:
                    continue
                if not resolved and (episode['state'] != 'active' or settings.notify_min_state == 'unacknowledged' and
                    (episode['acknowledged_at'] or now-episode['first_at'] < settings.notify_after_minutes*60)):
                    continue
                for channel in channels(settings,recipient):
                    phase = 'resolved' if resolved else 'opened'
                    self.enqueue(episode['id'],owner,channel,phase,payload(episode,settings.notify_site_url),now)
                    if resolved or not settings.notify_repeat_minutes or quiet_now(settings,now):
                        continue
                    with self.db:
                        sent_at = self.db.execute("SELECT MAX(last_at) FROM notification_deliveries WHERE episode=? AND owner=? AND channel=? AND state='sent'",
                                               (episode['id'],owner,channel)).fetchone()[0]
                        pending = self.db.execute("SELECT 1 FROM notification_deliveries WHERE episode=? AND owner=? AND channel=? AND state IN ('pending','sending')",
                                                  (episode['id'],owner,channel)).fetchone()
                    interval = settings.notify_repeat_minutes*60
                    if sent_at and sent_at+interval <= now and not pending:
                        phase = 'repeat:'+str(int(now//interval))
                        self.enqueue(episode['id'],owner,channel,phase,payload(episode,settings.notify_site_url,state='repeat'),now)

        with self.db:
            for owner,_,_,_ in recipients:
                self.db.execute('UPDATE notification_cursors SET last_planned=? WHERE owner=?',(now,owner))

    async def process(self,targets=(),now=None):
        """Claim a bounded batch outside network I/O transactions; stop after three attempts."""
        simulated = now is not None
        now = time.time() if now is None else now
        if quiet_now(self.settings(),now):
            return
        async with self.lock:
            recipients = {account_owner(target['identity']):target['choices'] for target in (targets or ())}
            with self.db:
                self.db.execute("UPDATE notification_deliveries SET state='failed',last_error='Delivery was interrupted during its final attempt.' "
                                "WHERE state='sending' AND attempts>=3 AND next_at<=?",(now,))
                rows = self.db.execute("SELECT id,episode,owner,channel,phase,payload,attempts,created FROM notification_deliveries "
                                       "WHERE state IN ('pending','sending') AND attempts<3 AND next_at<=? ORDER BY next_at,created,id LIMIT 20",(now,)).fetchall()
            for identifier,episode,owner,channel,phase,raw,attempts,created in rows:
                if owner != 'system' and targets is None:
                    with self.db:
                        self.db.execute('UPDATE notification_deliveries SET next_at=? WHERE id=?',(now+60,identifier))
                    continue  # The authority is unavailable; do not guess at current permissions.
                recipient = recipients.get(owner)
                fresh_since = 0
                if owner != 'system' and self.recipient_lookup is not None:
                    fresh = await self.recipient_lookup(owner)
                    if fresh is None:
                        with self.db:
                            self.db.execute('UPDATE notification_deliveries SET next_at=? WHERE id=?',(now+60,identifier))
                        continue
                    recipient,fresh_since = fresh.get('choices'),fresh.get('since',0)
                allowed = owner == 'system' or recipient is not None and channel in channels(self.settings(),recipient)
                if owner != 'system' and allowed:
                    with self.db:
                        since = self.db.execute('SELECT since FROM notification_cursors WHERE owner=?',(owner,)).fetchone()
                    allowed = (phase == 'notice' or recipient.get('notify_operational')) and (not since or created>=since[0]) and created>=fresh_since
                if owner == 'system':
                    allowed = channel in channels(self.settings())
                attempt_at = now if simulated else time.time()
                selected = self.settings()
                if quiet_now(selected,attempt_at):
                    break
                with self.db:
                    actual = self.db.execute('SELECT state,resolved_at,acknowledged_at,first_at FROM operational_alerts WHERE id=?',(episode,)).fetchone()
                    repeating = phase.startswith('repeat:')
                    delayed = actual and phase!='resolved' and selected.notify_min_state=='unacknowledged'
                    maturity = actual[3]+selected.notify_after_minutes*60 if delayed else 0
                    stale = actual and ((repeating and (actual[0]!='active' or actual[1] is not None or not selected.notify_repeat_minutes)) or
                                         delayed and (actual[2] or actual[1] is not None and actual[1]<maturity))
                    if not allowed or stale:
                        self.db.execute("UPDATE notification_deliveries SET state='skipped',last_error='' WHERE id=?",(identifier,))
                        continue
                    due = maturity
                    if repeating and selected.notify_repeat_minutes:
                        last_sent = self.db.execute("SELECT MAX(last_at) FROM notification_deliveries WHERE episode=? AND owner=? AND channel=? AND state='sent'",
                                                    (episode,owner,channel)).fetchone()[0]
                        due = max(due,(last_sent or attempt_at)+selected.notify_repeat_minutes*60)
                    if due>attempt_at:
                        # Revisit policy waits each collection tick so shortening a live delay takes effect.
                        self.db.execute('UPDATE notification_deliveries SET next_at=? WHERE id=?',(min(due,attempt_at+30),identifier))
                        continue
                    claimed = self.db.execute("UPDATE notification_deliveries SET state='sending',attempts=attempts+1,next_at=?,last_at=? "
                                              "WHERE id=? AND next_at<=? AND attempts<3 AND state IN ('pending','sending')",
                                              (attempt_at+60,attempt_at,identifier,attempt_at)).rowcount
                if not claimed:
                    continue
                failure = ''
                try:
                    await self.sender.send(channel,json.loads(raw),recipient,owner)
                except DeliveryError as problem:
                    failure = str(problem)
                except Exception as problem:
                    failure = 'Delivery failed ('+type(problem).__name__+').'  # Never retain provider exceptions.
                attempt = attempts+1
                ended = now if simulated else time.time()
                with self.db:
                    self.db.execute('UPDATE notification_deliveries SET state=?,next_at=?,last_at=?,last_error=? WHERE id=?',
                                    ('failed' if failure and attempt>=3 else 'pending' if failure else 'sent',
                                     ended+(60 if attempt==1 else 300),ended,failure,identifier))

    def status(self,owner='system',limit=20):
        """Read bounded notification delivery outcomes or credential presence without secrets."""
        with self.db:
            rows = self.db.execute('SELECT episode,channel,phase,state,attempts,last_at,last_error FROM notification_deliveries '
                                   'WHERE owner=? ORDER BY created DESC,id DESC LIMIT ?',(owner,limit)).fetchall()
        return [dict(zip(('episode','channel','phase','state','attempts','at','error'),row)) for row in rows]
