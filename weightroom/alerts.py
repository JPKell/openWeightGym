"""Durable alert episodes. Missing evidence never proves recovery.

Collectors pass complete, sanitized snapshots. A condition has one open episode;
acknowledgement belongs to that episode and never changes its condition state.
"""
import time
import uuid
import math
from datetime import datetime, timezone

from db import database, migrate
from db.schemas import ALERTS

COLUMNS = ('id', 'condition', 'source', 'title', 'explanation', 'state', 'first_at', 'last_at',
           'checked_at', 'resolved_at', 'recovery', 'acknowledged_by', 'acknowledged_at', 'manual')
OBSERVATION_BACKLOG_SECONDS = 60


class Alerts:
    """Durable operational alert episodes, acknowledgements and reviewed incidents."""
    def __init__(self, source):
        """Open durable alert storage and apply its portable schema migrations."""
        target = database(source)
        self.db = target.connect(check_same_thread=False)
        migrate(target, self.db, "weightroom.alerts", ALERTS)

    def observe(self, source, readings, now=None, missing_recovers=True):
        """Reconcile observed conditions without closing alerts on stale or missing evidence."""
        now = time.time() if now is None else now
        with self.db:
            opened = {row['condition']: row for row in self.rows(open_only=True) if row['source'] == source}
            available = readings is not None and all(row['active'] is not None for row in readings)
            if available and not missing_recovers:
                available = not (set(opened) - {row['key'] for row in readings})
            self.db.execute('''INSERT INTO alert_sources VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET checked_at=excluded.checked_at, available=excluded.available''',
                (source, now, int(available)))
            if readings is None:
                self.db.execute("UPDATE operational_alerts SET state='unknown', checked_at=? WHERE source=? AND resolved_at IS NULL AND manual=0", (now, source))
                return
            for reading in readings:
                key = reading['key']
                old = opened.pop(key, None)
                active = reading['active']
                if old and old['manual']:
                    continue  # an event's absence cannot undo it, including after review
                if active is None:
                    if old:
                        self.db.execute("UPDATE operational_alerts SET state='unknown', checked_at=? WHERE id=?", (now, old['id']))
                elif active:
                    if old:
                        self.db.execute("UPDATE operational_alerts SET state='active', title=?, explanation=?, last_at=?, checked_at=? WHERE id=?",
                            (reading['title'], reading['explanation'], now, now, old['id']))
                    else:
                        # Reviewed event IDs stay deduplicated even when polled again.
                        if reading.get('manual') and self.db.execute('SELECT 1 FROM operational_alerts WHERE condition=?', (key,)).fetchone():
                            continue
                        self.db.execute('''INSERT INTO operational_alerts
                            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, '', NULL, NULL, ?)''',
                            (uuid.uuid4().hex, key, source, reading['title'], reading['explanation'],
                             reading.get('at', now), now, now, int(reading.get('manual', False))))
                elif old:
                    self.db.execute("UPDATE operational_alerts SET state='recovered', resolved_at=?, checked_at=?, recovery=? WHERE id=?",
                        (now, now, reading.get('recovery', 'The latest reading is below the alert threshold.'), old['id']))
            for old in opened.values():
                if old['manual']:
                    continue
                if missing_recovers:
                    self.db.execute("UPDATE operational_alerts SET state='recovered', resolved_at=?, checked_at=?, recovery=? WHERE id=?",
                        (now, now, 'The condition is no longer configured in the complete source snapshot.', old['id']))
                else:
                    self.db.execute("UPDATE operational_alerts SET state='unknown', checked_at=? WHERE id=?", (now, old['id']))

    def rows(self, open_only=False, limit=100, offset=0):
        """List retained alert episodes and their current acknowledgement state."""
        where = ' WHERE resolved_at IS NULL' if open_only else ' WHERE resolved_at IS NOT NULL'
        sql = 'SELECT ' + ', '.join(COLUMNS) + ' FROM operational_alerts' + where + ' ORDER BY first_at DESC, id DESC'
        if not open_only:
            sql += ' LIMIT ? OFFSET ?'
        return [dict(zip(COLUMNS, row)) for row in self.db.execute(sql, () if open_only else (limit, offset)).fetchall()]

    def matching(self, filters, limit=None, offset=0):
        """Iterate matching episodes with portable, parameterized filtering."""
        where, values = self._where(filters)
        sql = 'SELECT ' + ', '.join(COLUMNS) + ' FROM operational_alerts WHERE ' + where + ' ORDER BY first_at DESC, id DESC'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            values += [limit, offset]
        cursor = self.db.execute(sql, values)
        try:
            for row in cursor:
                yield dict(zip(COLUMNS, row))
        finally:
            cursor.close()

    def count(self, filters):
        """Count the complete filtered set, independently of the visible page."""
        where, values = self._where(filters)
        return self.db.execute('SELECT COUNT(*) FROM operational_alerts WHERE ' + where, values).fetchone()[0]

    @staticmethod
    def _where(filters):
        scopes = {'open': 'resolved_at IS NULL', 'history': 'resolved_at IS NOT NULL', 'all': '1=1',
                  'unseen': 'resolved_at IS NULL AND acknowledged_at IS NULL',
                  'review': 'resolved_at IS NULL AND manual=1',
                  'unknown': "resolved_at IS NULL AND state='unknown'"}
        clauses, values = [scopes[filters['scope']]], []
        if filters['source']:
            clauses.append('source=?')
            values.append(filters['source'])
        if filters['q']:
            pattern = '%' + filters['q'].lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            fields = ('title', 'source', 'explanation', 'recovery', 'acknowledged_by')
            clauses.append('(' + ' OR '.join("LOWER(COALESCE(" + field + ", '')) LIKE ? ESCAPE '!'" for field in fields) + ')')
            values.extend([pattern] * len(fields))
        return ' AND '.join(clauses), values

    def summary(self):
        """Keep the KPI band scoped to all open episodes, including filtered-out rows."""
        row = self.db.execute('''SELECT COUNT(*),
            SUM(CASE WHEN acknowledged_at IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN manual=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END)
            FROM operational_alerts WHERE resolved_at IS NULL''').fetchone()
        return dict(zip(('open', 'unseen', 'review', 'unknown'), (value or 0 for value in row)))

    def source_names(self):
        """Include retained historical sources even if they no longer have a reading."""
        return [row[0] for row in self.db.execute('SELECT source FROM alert_sources UNION SELECT source FROM operational_alerts ORDER BY source')]

    def sources(self):
        """Read source freshness metadata for operational monitoring."""
        return [dict(zip(('source', 'checked_at', 'available'), row)) for row in
                self.db.execute('SELECT source, checked_at, available FROM alert_sources ORDER BY source').fetchall()]

    def acknowledge(self, identifier, person):
        """Record the authorized user's acknowledgement of an alert episode."""
        with self.db:
            return self.db.execute('''UPDATE operational_alerts SET acknowledged_by=?, acknowledged_at=?
                WHERE id=? AND acknowledged_at IS NULL''', (person, time.time(), identifier)).rowcount

    def review(self, identifier, person):
        """Retain the operator's review of a recorded memory-kill incident."""
        with self.db:
            return self.db.execute("""UPDATE operational_alerts SET state='reviewed', resolved_at=?, recovery=?
                WHERE id=? AND resolved_at IS NULL AND manual=1""",
                (time.time(), f'Reviewed by {person}; this records review, not proof of memory recovery.', identifier)).rowcount


def machine_conditions(reading, threshold):
    """Translate current machine readings into configured operational alert conditions."""
    services, heat = [], []
    for service in reading['services']:
        state, result = service['state'], service.get('result', '')
        # An inactive unit can retain an earlier unsuccessful Result after an
        # intentional stop. Only the current failed state proves service failure.
        active = True if state == 'failed' else False if state == 'active' or (state == 'inactive' and result == 'success') else None
        services.append({'key': 'service:' + service['unit'], 'active': active,
                         'title': service['name'] + ' service failed',
                         'explanation': f"{service['unit']}: {state}; result {result or 'unavailable'}.",
                         'recovery': f"{service['unit']}: {state}; {'cleanly stopped' if state == 'inactive' else 'running'}; result {result or 'unavailable'}."})
    for card in reading['cards']:
        value = card.get('temperature')
        heat.append({'key': f"gpu:{card['card']}", 'active': value >= threshold if isinstance(value, (int, float)) else None,
                     'title': f"GPU {card['card']} temperature", 'explanation': f'{value}°C; alert threshold {threshold}°C.',
                     'recovery': f'{value}°C is below {threshold}°C.'})
    return services, heat


def observation_evidence(row, suffix, now, baseline, lost, hold_seconds):
    """Describe the actual health snapshot without inferring a service failure."""
    from loadcoach.observation_store import SOURCE_FRESH_SECONDS
    name = row['machine']
    recording = 'true' if row.get('recording') is True else 'false' if row.get('recording') is False else 'not reported'
    origin = ("the control node's local Turnstile observation writer" if row['_local'] else
              f"LoadCoach on node {name}, forwarded to the control node's Turnstile through signed POST /turnstile/log")
    facts = f"Observation health from {origin}: fresh={'true' if row.get('fresh') else 'false'}; recording={recording}. "
    received = row.get('received')
    if not row['_local']:
        if type(received) in (int, float) and math.isfinite(received):
            try:
                stamp = datetime.fromtimestamp(received, timezone.utc).strftime('%d %b %Y, %H:%M:%S UTC')
                facts += f"Control node last received this node's health report at {stamp} ({max(0, now-received):.1f} seconds before this check). "
            except (OverflowError, ValueError, OSError):
                facts += 'The last health receipt time is unavailable. '
        else:
            facts += 'The report did not provide a last health receipt time. '
    if suffix == 'freshness':
        if row['_local']:
            return facts + 'This check uses the presence of local writer health in the control node\'s metrics report. No service-status probe was made.'
        return facts + (f'A source is fresh only while its last receipt is less than {SOURCE_FRESH_SECONDS} seconds old '
                        'and recording=true. The report does not identify whether the node is asleep, the sender stopped, '
                        'forwarding failed or recording is unavailable; it does not prove that a service crashed.')
    if suffix == 'backlog':
        return facts + (f"The last reported forwarding backlog contains {row.get('backlog_count', 0)} observations; "
                        f"the oldest is {row.get('backlog_age_seconds', 0):g} seconds old. "
                        f'An alert requires a nonempty backlog at least {OBSERVATION_BACKLOG_SECONDS} seconds old and fresh source evidence. '
                        'Compare subsequent reports to see whether forwarding is draining it.')
    reasons = ', '.join(f'{key}={row.get("dropped", {}).get(key, 0)}' for key in
                        ('queue_full', 'spool_full', 'writer_error', 'shutdown') if row.get('dropped', {}).get(key))
    return facts + (f'Reported process-lifetime loss count {lost}; previous count for this process {baseline}. '
                    + (f'Reason counters: {reasons}. ' if reasons else '')
                    + f'The alert stays open for {hold_seconds:g} seconds after an observed increase. '
                    'A process replacement resets the comparison baseline; sampled successful observations are excluded.')


def observation_conditions(report, previous=None, now=None, error_percent=0, p95_seconds=0, min_samples=100, hold_seconds=60):
    """Detect fresh observation loss/backlogs; old loss counters do not imply a new incident."""
    if report is None or 'local' not in report:
        return None, previous or {}
    now = time.time() if now is None else now
    previous = previous or {}
    latest, conditions = {}, []
    rows = [{'machine':'front', 'fresh':report.get('local') is not None, **(report.get('local') or {}), '_local':True},
            *(dict(row, _local=False) for row in report.get('sources', []))]
    for row in rows:
        name = row['machine']
        process = row.get('process', '')
        counts = row.get('dropped', {})
        lost = sum(value for key,value in counts.items() if key != 'sampled_success' and type(value) is int)
        old = previous.get(name)
        # A restart resets counters; its first snapshot reports health, not an invented delta.
        baseline = old.get('lost', 0) if old and old.get('process') == process else 0
        rising = lost > baseline
        loss_at = now if rising else (old or {}).get('loss_at', 0)
        latest[name] = {'process':process, 'lost':lost, 'loss_at':loss_at}
        fresh = row.get('fresh', False)
        for suffix, active, title in (
            ('loss', bool(loss_at and now-loss_at < hold_seconds) if fresh else None, 'Optional observation data loss'),
            ('backlog', row.get('backlog_count', 0) > 0 and row.get('backlog_age_seconds', 0) >= OBSERVATION_BACKLOG_SECONDS if fresh else None,
             'Observation forwarding backlog'),
            ('freshness', not fresh, 'Observation source unavailable')):
            conditions.append({'key':'observation:'+name+':'+suffix, 'active':active,
                               'title':title+' · '+name,
                               'explanation':observation_evidence(row, suffix, now, baseline, lost, hold_seconds),
                               'recovery':'A fresh snapshot confirms the condition has cleared.'})
    rules = dict(previous.get('_rules', {}))
    for group in report.get('groups', []):
        if group['metric'] != 'seconds' or group['operation'] != 'ingress':
            continue
        name = group['service']
        enough = group['count'] >= min_samples
        rate = group['outcomes'].get('error', 0)*100/max(1, group['count'])
        for rule, enabled, above, title in (
            ('error-rate', error_percent > 0, rate >= error_percent, 'Sustained request error rate'),
            ('latency', p95_seconds > 0, group.get('p95') is not None and group['p95'] >= p95_seconds,
             'Sustained approximate request p95 latency')):
            if not enabled:
                continue
            key = 'observation:'+name+':'+rule
            old = rules.get(key, {'above':None, 'since':now})
            state = above if enough and (rule != 'latency' or group.get('p95') is not None) else None
            if state != old['above']:
                old = {'above':state, 'since':now}
            rules[key] = old
            active = state if state is not None and now-old['since'] >= hold_seconds else None
            measurement = (f"{group['outcomes'].get('error', 0)} recorded errors / {group['count']} ingress calls = {rate:.2f}%; "
                           f'configured threshold {error_percent:g}%') if rule == 'error-rate' else (
                           f"approximate p95 {group['p95'] if group.get('p95') is not None else 'unavailable'} seconds; configured threshold {p95_seconds:g} seconds. "
                           'Histogram p95 is an upper-bound estimate')
            window = (f"Window: {report['until']-report['since']:g} seconds. "
                      if type(report.get('since')) in (int, float) and type(report.get('until')) in (int, float) else '')
            conditions.append({'key':key, 'active':active, 'title':title+' · '+name,
                               'explanation':f'Service {name}: {measurement}. {window}'
                                   f'Minimum sample count {min_samples}; observed count {group["count"]}. '
                                   f'The measured condition must persist for {hold_seconds:g} seconds. '
                                   'WeightRoom reads the retained ingress metric window from the control node\'s Turnstile; benchmarks are excluded.',
                               'recovery':'A sustained fresh measured window is below the configured threshold.'})
    latest['_rules'] = rules
    return conditions, latest
