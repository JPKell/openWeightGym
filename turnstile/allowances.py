"""Advisory USD allowances over retained ordinary request records, never admission rules."""
import json
import math
import time
from decimal import Decimal

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.usage import ENDINGS, amount
from loadcoach.users import key_identity
from .accounts import address, error, read_json, signed_in


class Allowances:
    """One small policy table beside accounts; the existing event log owns every charge."""

    def __init__(self, users, log):
        """Bind advisory spending policies to their retained account store."""
        self.users, self.log = users, log

    def choices(self):
        """Current immutable users/keys and named groups; deleted targets never reattach."""
        choices = []
        accounts = {name: (identity, group) for name, identity, group in self.users.db.execute(
            'SELECT name,identity,grp FROM users')}
        for name, (identity, group) in accounts.items():
            choices.append(dict(scope='user', subject=identity, name=name, owner=identity, group=group))
        for name in self.users.groups:
            choices.append(dict(scope='group', subject=name, name=name, owner='', group=name))
        for verifier, name, label in self.users.db.execute('SELECT hash,user,label FROM keys WHERE expires IS NULL'):
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
        """Changing an advisory target also requires permission to inspect its accounting."""
        ability = 'manage-groups' if target['scope']=='group' else 'manage-users'
        return user.can('see-log') and user.can(ability)

    def save(self, user, scope, subject, value):
        """Replace a target atomically; zero disables it. Nothing reserves or refuses inference."""
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
        now = time.time() if now is None else now
        since = now - 86400
        targets = [t for t in self.choices() if self.visible(user, t)]
        policies = {(scope, subject): limit for scope, subject, limit in self.users.db.execute(
            'SELECT scope,subject,amount FROM spending_allowances')}
        totals = {(t['scope'], t['subject']): dict(recorded=Decimal(0), requests=0, unknown_costs=0, unresolved=0) for t in targets}
        marks = ','.join('?' for _ in ENDINGS)
        # Every ending includes snapshots on matching compute. Keep only the newest retained
        # ending for each parent in this window, so multiple endings are not charged twice.
        records = self.log.db.execute(f"SELECT id,at,what,\"user\",detail,machine FROM events "
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
        # Unresolved means no retained ending, not proof that the request is still executing.
        # The unary plus on e.at keeps the planner on the parent index; scanning every earlier
        # request by time took 41 s on 48k events and stalled turnstile's event loop.
        ending_parent = self.log.json_value('e.detail', 'of', 'integer')
        records = self.log.db.execute(f"SELECT a.detail FROM events a WHERE a.kind='request' AND a.what='asked' "
            f"AND a.at>=? AND a.at<=? AND NOT EXISTS (SELECT 1 FROM events e WHERE e.kind='request' "
            f"AND e.what IN ({marks}) AND +e.at<=? AND {ending_parent}=+COALESCE(a.source,a.id) "
            "AND COALESCE(e.machine,'')=COALESCE(a.machine,''))", (since,now,*ENDINGS,now))
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
                'manage':self.manageable(user,target)})
        return dict(mode='soft', currency='USD', window_seconds=86400, as_of=now, since=since,
            rows=rows, legacy_records=legacy, unattributed_keys=unattributed_keys, keep_days=self.log.keep_days)


def routes(users, log, caller, ask_compute):
    """Authenticated front-owned reporting and management; compute is never woken."""
    allowances = Allowances(users, log)

    async def handle(request, user):
        """Read or change advisory spending allowances under current caller permissions."""
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
            return JSONResponse({'saved':True,'mode':'soft'},headers={'cache-control':'no-store'})
        synchronized = await ask_compute(user,'POST','/loadcoach/send-log')
        return JSONResponse({**allowances.report(user),'compute_synchronized':synchronized is not None and synchronized.status_code==200},
            headers={'cache-control':'no-store'})

    checked = signed_in(caller,log,handle)
    async def private(request):
        """Add no-store policy to account-specific allowance responses."""
        response = await checked(request)
        response.headers['cache-control'] = 'no-store'
        return response
    return [Route('/loadcoach/allowances',private,methods=['GET','POST'])]
