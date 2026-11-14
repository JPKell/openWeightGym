"""Front-owned Responses records, current-clearance access and bounded history."""

import copy
import json
import secrets
import time

from db import database, migrate
from db.schemas import RESPONSES
from loadcoach.groups import LEVELS
from weightroom.extraction import Problem
from weightroom.extraction_store import encoded

MAX_RECORDS = 10_000
MAX_BYTES = 256 * 1024 * 1024
COLUMNS = 'id,owner,owner_name,level,created,expires,status,model,previous,request_id,request,response,preview,stored_size'


def owner_of(user):
    """Use the stable identity resolved from this request's bearer key."""
    return user.identity or user.name


def normalized_input(value):
    """Give saved input items stable IDs without changing the caller's objects."""
    items = [{'role': 'user', 'content': value}] if isinstance(value, str) else copy.deepcopy(value)
    for item in items:
        item.setdefault('type', 'message')
        item.setdefault('id', 'msg_' + secrets.token_hex(16))
    return items


def input_preview(items):
    """Extract bounded plain text for filtering without parsing response JSON in SQL."""
    for item in items:
        content = item.get('content', '')
        if isinstance(content, str) and content:
            return content[:1000]
        if isinstance(content, list):
            text = ' '.join(part.get('text', '') for part in content if part.get('type') in ('input_text', 'output_text'))
            if text:
                return text[:1000]
    return ''


class Responses:
    """Store lifecycle data beside front accounts, included in existing backups and transfers."""
    def __init__(self, source, keep_days=lambda: 30):
        """Open the shared accounts database and migrate the owning Turnstile stream."""
        source = database(source)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, 'turnstile.responses', RESPONSES)
        self.keep_days = keep_days

    def prune(self):
        """Remove expired records and apply a newly shortened retention setting."""
        now = time.time()
        with self.db:
            self.db.execute('DELETE FROM responses WHERE expires<=? OR created<=?', (now, now - self.keep_days()*86400))

    def capacity(self, owner, extra=0):
        """Refuse new stored state before inference when the per-account quota is full."""
        count, size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(stored_size),0) FROM responses WHERE owner=? AND expires>?',
                                      (owner, time.time())).fetchone()
        if count >= MAX_RECORDS or size + extra > MAX_BYTES:
            raise Problem('Stored Responses limit reached (10,000 records or 256 MiB). Delete older responses or send store:false.', 409, 'storage_limit')

    def save(self, user, request, response, level, request_id=None):
        """Save only completed attempts, including failed or incomplete final objects."""
        asked, result = encoded(request), encoded(response)
        size = len(asked.encode()) + len(result.encode())
        now = time.time()
        with self.db:
            self.capacity(owner_of(user), size)
            self.db.execute('INSERT INTO responses (' + COLUMNS + ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (response['id'], owner_of(user), user.name, level, now, now + self.keep_days()*86400,
                 response['status'], response['model'], request.get('previous_response_id'), str(request_id) if request_id else None,
                 asked, result, input_preview(request['input']), size))

    def one(self, user, identifier, *, delete=False, own=False):
        """Return the same 404 for absent, foreign, expired or above-clearance records."""
        row = self.db.execute('SELECT ' + COLUMNS + ' FROM responses WHERE id=? AND expires>? AND created>?',
                              (identifier, time.time(), time.time()-self.keep_days()*86400)).fetchone()
        found = dict(zip(COLUMNS.split(','), row)) if row else None
        ability = 'manage-users' if delete else 'see-log'
        if (not found or LEVELS.index(found['level']) > LEVELS.index(user.group.max_sensitivity)
                or (found['owner'] != owner_of(user) and (own or ability not in user.group.can))):
            raise Problem('Response not found.', 404, 'response_not_found')
        for key in ('request', 'response'):
            found[key] = json.loads(found[key])
        return found

    def remove(self, user, identifier):
        """Delete an authorized record without broadening read access."""
        self.one(user, identifier, delete=True)
        with self.db:
            self.db.execute('DELETE FROM responses WHERE id=?', (identifier,))

    def forget(self, owner):
        """Erase responses when an account's stable identity is deleted."""
        with self.db:
            self.db.execute('DELETE FROM responses WHERE owner=?', (owner,))

    def history(self, user, identifier):
        """Rebuild at most fifty same-owner inputs and outputs, dropping old instructions."""
        records, visited = [], set()
        while identifier:
            if identifier in visited or len(records) >= 50:
                raise Problem('A Responses chain may contain at most 50 prior responses.', 400, 'chain_limit')
            visited.add(identifier)
            record = self.one(user, identifier, own=True)
            records.append(record)
            identifier = record['previous']
        items = []
        level = 'normal'
        for record in reversed(records):
            level = max(level, record['level'], key=LEVELS.index)
            from turnstile.responses import chat_request
            own = [{key: value for key, value in item.items() if key != 'id'} for item in record['request']['input']]
            items += chat_request({'model': record['model'], 'input': own, 'store': False})['messages']
            for item in record['response']['output']:
                if item['type'] == 'message':
                    content = ''.join(part.get('text', part.get('refusal', '')) for part in item['content'])
                    items.append({'role': 'assistant', 'content': content})
        return items, level

    def listing(self, user, filters, *, limit=25, offset=0):
        """Page scoped previews in SQL, sharing the exact predicate with the total count."""
        levels = LEVELS[:LEVELS.index(user.group.max_sensitivity)+1]
        where = ['expires>?', 'created>?', 'level IN (' + ','.join('?' for _ in levels) + ')']
        values = [time.time(), time.time()-self.keep_days()*86400, *levels]
        if 'see-log' not in user.group.can or filters.get('scope', 'own') != 'all':
            where.append('owner=?'); values.append(owner_of(user))
        for key in ('model', 'status'):
            if filters.get(key):
                where.append(key+'=?'); values.append(filters[key])
        if 'see-log' in user.group.can and filters.get('user'):
            where.append('owner_name=?'); values.append(filters['user'])
        if filters.get('owners') is not None:
            owners = filters['owners']
            where.append('owner_name IN (' + ','.join('?' for _ in owners) + ')' if owners else '1=0')
            values.extend(owners)
        for key, op in (('since', '>='), ('until', '<=')):
            if filters.get(key):
                where.append('created'+op+'?'); values.append(filters[key])
        if filters.get('q'):
            where.append("LOWER(preview) LIKE ? ESCAPE '!' ")
            text = filters['q'].lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
            values.append('%'+text+'%')
        clause = ' AND '.join(where)
        total = int(self.db.execute('SELECT COUNT(*) FROM responses WHERE '+clause, values).fetchone()[0])
        rows = self.db.execute('SELECT '+COLUMNS+' FROM responses WHERE '+clause+' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                               (*values, limit, offset)).fetchall()
        records = []
        for row in rows:
            record = dict(zip(COLUMNS.split(','), row))
            record['response'] = json.loads(record['response'])
            records.append(record)
        return records, total

    def next(self, user, identifier):
        """Link only to visible continuations of this response."""
        rows = self.db.execute('SELECT id FROM responses WHERE previous=? ORDER BY created,id LIMIT 100', (identifier,)).fetchall()
        answer = []
        for row in rows:
            try:
                answer.append(self.one(user, row[0]))
            except Problem:
                pass
        return answer
