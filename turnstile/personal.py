"""Private, bounded in-app account notices from the authoritative retained records."""

import time


def notices(users, log, user, spending=None):
    """Return this account's enabled notice categories without inference content."""
    choices = users.choices(user.name)
    since = time.time() - 7 * 86400
    seen = choices['notices_read_at']
    items = []
    if choices['alert_security']:
        target = log.json_value('detail', 'name')
        rows = log.db.execute("SELECT id,at,what FROM events WHERE at>=? "
                              f"AND COALESCE({target},\"user\")=? AND ((kind='user' AND what IN "
                              "('signed-in','changed','key-added','key-removed','session-ended','sessions-ended')) OR (kind='sign-in' AND what='throttled')) "
                              "ORDER BY at DESC,id DESC LIMIT 25", (since, user.name))
        labels = {'signed-in':'New sign-in', 'changed':'Account settings changed', 'key-added':'API key created',
                  'key-removed':'API key removed', 'session-ended':'Sign-in ended', 'sessions-ended':'Other sign-ins ended',
                  'throttled':'Sign-in attempts throttled'}
        items += [dict(id='security-' + str(number), at=at, title=labels[what], category='Security',
                       href='/account/logs', unread=at > seen) for number, at, what in rows]
    if choices['alert_jobs']:
        rows = log.db.execute("SELECT id,at,kind,what FROM events WHERE \"user\"=? AND at>=? AND "
                              "((kind='batch' AND what IN ('completed','failed','cancelled','expired')) "
                              "OR (kind='soundroom' AND what='completed')) ORDER BY at DESC,id DESC LIMIT 25", (user.name, since))
        items += [dict(id='job-' + str(number), at=at, title=('Audio generation' if kind == 'soundroom' else 'Batch') + ' ' + what,
                       category='Work', href='/account/logs', unread=at > seen) for number, at, kind, what in rows]
    # A manual backup's outcome reaches whoever started it, whatever alert_jobs says: they asked for it then.
    rows = log.db.execute("SELECT id,at,what FROM events WHERE \"user\"=? AND at>=? AND kind='backup' "
                          "AND what IN ('completed','failed') ORDER BY at DESC,id DESC LIMIT 25", (user.name, since))
    items += [dict(id='backup-' + str(number), at=at, title='Backup ' + what, category='Work', href='/backups',
                   unread=at > seen) for number, at, what in rows]
    # Avoid a full accounting report when this account has no applicable advisory policy.
    from loadcoach.users import key_identity
    owned_keys = {key_identity(verifier) for (verifier,) in users.db.execute(
        "SELECT hash FROM keys WHERE user=? AND kind='api'", (user.name,))}
    def own_policy(scope, subject):
        """Limit personal spending notices to owned account, group or key scopes."""
        return ((scope == 'user' and subject == user.identity) or
                (scope == 'group' and subject == user.group_name) or (scope == 'key' and subject in owned_keys))
    applicable = any(own_policy(scope, subject) for scope, subject in users.db.execute(
        'SELECT scope,subject FROM spending_allowances WHERE amount>0'))
    from .allowances import Allowances
    spending = spending or Allowances(users, log)
    threshold = 80 if spending.setting('allowance_mode') == 'advisory' else spending.threshold()
    if choices['alert_spending'] and applicable and threshold:
        report = spending.report(user)
        for row in report['rows']:
            if not own_policy(row['scope'], row['subject']):
                continue
            if row['allowance'] and row['recorded'] >= row['allowance'] * threshold / 100:
                field = {'user':'account_id', 'group':'group', 'key':'key_id'}[row['scope']]
                last = log.db.execute("SELECT MAX(at) FROM events WHERE kind='request' AND at>=? AND " +
                                      log.json_value('detail', field) + '=?', (report['since'], row['subject'])).fetchone()[0]
                items.append(dict(id='spending-' + row['scope'] + '-' + row['subject'], at=last, category='Spending',
                                  title=f"{row['name']} recorded spending is at "
                                        f"{row['recorded'] / row['allowance']:.0%} of its {report['window_label']} {report['mode']} allowance.",
                                  href='/allowances', unread=bool(last and last > seen)))
    items.sort(key=lambda item: item['at'] or time.time(), reverse=True)
    return {'items': items[:50], 'unread': sum(item['unread'] for item in items[:50]), 'since': since,
            'retention_days': log.keep_days}
