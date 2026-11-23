"""Recorded USD allowances with live advisory, warning and hosted-only admission policy."""
import asyncio
import json
import math
import time
from decimal import Decimal
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo

from loadcoach.setting_catalog import default_for
from weightroom.display import configured_timezone

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.usage import ENDINGS, amount
from loadcoach.users import key_identity
from .accounts import address, error, read_json, signed_in
from .reporting import ReportTimeout, run as run_report

FLUSH_SECONDS = 5  # as long as the Usage report waits for a node's log
ENDING_SLACK = 86400  # seconds an ending's clock may run behind its request's


class Allowances:
    """One small policy table beside accounts; the existing event log owns every charge."""

    def __init__(self, users, log, *, setting=None, timezone=lambda: "", threshold=lambda: 0):
        """Bind recorded spending policies to their retained account store."""
        self.users, self.log = users, log
        self.setting = setting or (lambda name: default_for("turnstile", name))
        self.timezone, self.threshold = timezone, threshold

    def window(self, now):
        """Resolve the current accounting window in the configured control node time zone."""
        if self.setting('allowance_period') == 'rolling_24h':
            return now - 86400, None, 'rolling 24 hours'
        local = datetime.fromtimestamp(now, ZoneInfo(configured_timezone(self.timezone())))
        first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month = first.month % 12 + 1
        next_month = first.replace(year=first.year + (month == 1), month=month)
        return first.timestamp(), next_month.timestamp(), 'calendar month'

    def applicable(self, user, row):
        """Only this account, its current API key and its recorded group can gate a request."""
        return row['allowance'] and ((row['scope'] == 'user' and row['subject'] == user.identity) or
            (row['scope'] == 'key' and row['subject'] == user.key_identity) or
            (row['scope'] == 'group' and row['subject'] == user.group_name))

    def admission(self, user):
        """Return a refusal or warning metadata for a hosted attempt; never reserve money."""
        mode = self.setting('allowance_mode')
        if mode == 'advisory' or not self.users.db.execute('SELECT 1 FROM spending_allowances WHERE amount>0 LIMIT 1').fetchone():
            return None, {}
        report = self.report(user)
        rows = [row for row in report['rows'] if self.applicable(user, row)]
        if mode == 'block':
            for row in rows:
                if row['recorded'] >= row['allowance']:
                    when = datetime.fromtimestamp(row['reset_at'], utc_timezone.utc).isoformat(timespec='seconds')
                    reset = 'resets at' if report['period'] == 'calendar_month' else 'next recorded charge leaves the rolling window at'
                    return {'message': f"The {row['scope']} allowance {row['name']} is exhausted (${row['recorded']:.6f} recorded / ${row['allowance']:.6f}); {reset} {when}.",
                            'code': 'allowance_exceeded'}, {}
        if mode == 'warn' and rows:
            return None, {'x-suite-allowance': json.dumps([
                {key: row[key] for key in ('scope', 'name', 'recorded', 'allowance', 'remaining', 'reset_at')} for row in rows],
                ensure_ascii=True, separators=(',', ':'))}
        return None, {}

    def choices(self):
        """Current immutable users/keys and named groups; deleted targets never reattach."""
        choices = []
        accounts = {name: (identity, group) for name, identity, group in self.users.db.execute(
            'SELECT name,identity,grp FROM users')}
        for name, (identity, group) in accounts.items():
            choices.append(dict(scope='user', subject=identity, name=name, owner=identity, group=group))
        for name in self.users.groups:
            choices.append(dict(scope='group', subject=name, name=name, owner='', group=name))
        for verifier, name, label in self.users.db.execute("SELECT hash,user,label FROM keys WHERE kind='api'"):
            if name in accounts:
                identity, group = accounts[name]
                choices.append(dict(scope='key', subject=key_identity(verifier),
                    name=name+' / '+(label or 'Unlabelled key')+' / '+verifier[:8], owner=identity, group=group))
        return choices

    @staticmethod
    def visible(user, target):
        """Members see their pooled allowance, never another member's records or keys."""
        return user.can('see-log') or (target['scope']=='group' and target['subject']==user.group_name) or (
            target['scope']!='group' and target['owner']==user.identity)

    @staticmethod
    def manageable(user, target):
        """Changing an allowance target also requires permission to inspect its accounting."""
        ability = 'manage-groups' if target['scope']=='group' else 'manage-users'
        return user.can('see-log') and user.can(ability)

    def save(self, user, scope, subject, value):
        """Replace a target atomically; zero disables it. No money is reserved."""
        target = next((t for t in self.choices() if t['scope']==scope and t['subject']==subject), None)
        if target is None:
            raise ValueError('Choose a current user, group or persistent API key.')
        if not self.manageable(user, target):
            raise PermissionError('Managing this allowance requires event-log access and the matching account/group management ability.')
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1_000_000_000:
            raise ValueError('Allowance must be a finite number from 0 to 1,000,000,000 USD; zero disables it.')
        with self.users.db:
            if value == 0:
                self.users.db.execute('DELETE FROM spending_allowances WHERE scope=? AND subject=?', (scope, subject))
            else:
                self.users.db.execute('INSERT INTO spending_allowances (scope,subject,amount) VALUES (?,?,?) '
                    'ON CONFLICT(scope,subject) DO UPDATE SET amount=excluded.amount', (scope, subject, value))
        return target

    def report(self, user, now=None):
        """Latest ending per request is charged once; missing costs are never mock-priced."""
        return self.tally(user, self.prepare(user, now), self.log)

    def prepare(self, user, now=None):
        """What the report needs from the account store: its window, visible targets and policies."""
        now = time.time() if now is None else now
        targets = [t for t in self.choices() if self.visible(user, t)]
        policies = {(scope, subject): limit for scope, subject, limit in self.users.db.execute(
            'SELECT scope,subject,amount FROM spending_allowances')}
        return now, targets, policies

    def tally(self, user, prepared, log):
        """Charge the prepared targets from `log` alone, so a bounded read-only reader can run it off the event loop."""
        now, targets, policies = prepared
        since, period_end, window_label = self.window(now)
        totals = {(t['scope'], t['subject']): dict(recorded=Decimal(0), requests=0, unknown_costs=0, unresolved=0, next_charge=None) for t in targets}
        marks = ','.join('?' for _ in ENDINGS)
        # Every ending includes snapshots on matching compute. Keep only the newest retained
        # ending for each parent in this window, so multiple endings are not charged twice.
        records = log.db.execute(f"SELECT id,at,what,\"user\",detail,machine FROM events "
            f"WHERE kind='request' AND what IN ({marks}) AND at>=? AND at<=? ORDER BY id DESC", (*ENDINGS,since,now))
        seen = set()
        legacy = 0
        unattributed_keys = 0
        for number, at, what, name, raw, machine in records:
            data = json.loads(raw)
            parent = data.get('of')
            request = (machine or '', parent if type(parent) is int and parent>0 else 'event:'+str(number))
            if request in seen:
                continue
            seen.add(request)
            if not data.get('account_id') and (user.can('see-log') or name==user.name):
                legacy += 1
            if data.get('account_id') and not data.get('key_id') and (user.can('see-log') or data['account_id']==user.identity):
                unattributed_keys += 1
            for key in (('user',data.get('account_id')), ('key',data.get('key_id')), ('group',data.get('group'))):
                if key not in totals:
                    continue
                row = totals[key]
                row['requests'] += 1
                cost = amount(data.get('cost'))
                if cost is None:
                    row['unknown_costs'] += 1
                else:
                    row['recorded'] += cost
                    if cost > 0:
                        row['next_charge'] = min(row['next_charge'] or at, at)
        # Unresolved means no retained ending, not proof that the request is still executing.
        # The unary plus on e.at keeps the planner on the parent index; scanning every earlier
        # request by time took 41 s on 48k events and stalled turnstile's event loop.
        # An ending comes after its request, so only endings from the window (less a day for
        # clocks that disagree) are compared: PostgreSQL otherwise hashed every ending ever kept (QA-073).
        ending_parent = log.json_value('e.detail', 'of', 'integer')
        records = log.db.execute(f"SELECT a.detail FROM events a WHERE a.kind='request' AND a.what='asked' "
            f"AND a.at>=? AND a.at<=? AND NOT EXISTS (SELECT 1 FROM events e WHERE e.kind='request' "
            f"AND e.what IN ({marks}) AND +e.at<=? AND +e.at>=? AND {ending_parent}=+COALESCE(a.source,a.id) "
            "AND COALESCE(e.machine,'')=COALESCE(a.machine,''))", (since,now,*ENDINGS,now,since-ENDING_SLACK))
        for (raw,) in records:
            data = json.loads(raw)
            for key in (('user',data.get('account_id')), ('key',data.get('key_id')), ('group',data.get('group'))):
                if key in totals:
                    totals[key]['unresolved'] += 1
        rows = []
        for target in targets:
            key = target['scope'],target['subject']
            value = totals[key]
            limit = policies.get(key, 0)
            recorded = float(value['recorded'])
            difference = Decimal(str(limit))-value['recorded']
            rows.append({**target, **value, 'recorded':recorded, 'allowance':limit,
                'remaining':float(max(Decimal(0), difference)) if limit else None,
                'over_by':float(max(Decimal(0), -difference)) if limit else None,
                'reset_at':period_end or ((value['next_charge'] or now) + 86400),
                'manage':self.manageable(user,target)})
        return dict(mode=self.setting('allowance_mode'), period=self.setting('allowance_period'), window_label=window_label,
            reset_at=period_end, currency='USD', window_seconds=(period_end - since) if period_end else 86400, as_of=now, since=since,
            rows=rows, legacy_records=legacy, unattributed_keys=unattributed_keys, keep_days=log.keep_days)


def routes(users, log, caller, ask_compute, *, service=None):
    """Authenticated reporting and management of allowances the control node keeps; compute is never woken."""
    allowances = service or Allowances(users, log)

    async def handle(request, user):
        """Read or change recorded spending allowances under current caller permissions."""
        if request.method=='POST':
            body = await read_json(request, {'scope','subject','amount'})
            if isinstance(body, Response):
                return body
            try:
                target = allowances.save(user,body.get('scope'),body.get('subject'),body.get('amount'))
            except PermissionError as why:
                return error(403,str(why))
            except ValueError as why:
                return error(400,str(why))
            log.add('allowance','changed',user.name,address(request),scope=target['scope'],subject=target['subject'],amount=body['amount'])
            return JSONResponse({'saved':True,'mode':allowances.setting('allowance_mode')},headers={'cache-control':'no-store'})
        # A flush is bounded as the Usage report's is, and the tally reads its own connection in a
        # worker thread: run on the event loop it held up every concurrent report past its deadline (QA-073).
        try:
            synchronized = await asyncio.wait_for(ask_compute(user,'POST','/loadcoach/send-log'), timeout=FLUSH_SECONDS)
        except TimeoutError:
            synchronized = None
        prepared = allowances.prepare(user)
        try:
            result = await run_report(log, lambda view: allowances.tally(user, prepared, view))
        except ReportTimeout:
            return error(503, 'The allowance report took too long. Retry shortly.')
        return JSONResponse({**result,'compute_synchronized':synchronized is not None and synchronized.status_code==200},
            headers={'cache-control':'no-store'})

    checked = signed_in(caller,log,handle)
    async def private(request):
        """Add no-store policy to account-specific allowance responses."""
        response = await checked(request)
        response.headers['cache-control'] = 'no-store'
        return response
    return [Route('/loadcoach/allowances',private,methods=['GET','POST'])]
