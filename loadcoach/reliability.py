"""Request outcomes and bounded role preferences, isolated by identity and fingerprint.

No prompt or answer text lives here. A quality rating keeps the rater's own explanation of an answer, never the answer. Recovery is an operator-initiated request through
the normal admission path; observing or previewing reliability never runs inference.
"""

import json
import time
from collections import Counter

from db import adopt, migrate
from db.schemas import RELIABILITY

FAULTS = frozenset({'error', 'timeout', 'empty'})
# Said by compute with each answer, for turnstile, which keeps reliability on the front machine: the
# model that answered, and the identity and fingerprint its reliability is kept under.
ANSWERED = ('x-loadcoach-answered', 'x-loadcoach-answered-id', 'x-loadcoach-answered-fingerprint')
OUTCOME = 'x-loadcoach-outcome'  # how a request ended, when compute knows it before answering: it was refused, or would not load
WINDOW = 7 * 86400
COOLDOWN = 300
EXPLANATION = 2000  # the longest explanation a rating may carry, in characters


def answer_outcome(raw: bytes, content_type: str, path: str) -> str:
    """Judge chat/completion envelopes, including streams and tool-only answers.

    Other endpoints and unknown successful envelopes are not scored as empty answers.
    Reasoning alone is not a usable final answer.
    """
    if path not in ('/v1/chat/completions', '/v1/completions'):
        return 'success'
    try:
        pieces = [json.loads(line[5:]) for line in raw.decode().splitlines()
                  if line.startswith('data:') and line[5:].strip() != '[DONE]'] if 'text/event-stream' in content_type else [json.loads(raw)]
        recognized = 'text/event-stream' in content_type and b'data: [DONE]' in raw
        for piece in pieces:
            if 'choices' not in piece:
                continue
            recognized = True
            for choice in piece['choices']:
                message = choice.get('message') or choice.get('delta') or {}
                if str(choice.get('text') or message.get('content') or '').strip() or message.get('tool_calls') or message.get('function_call'):
                    return 'success'
        return 'empty' if recognized else 'unscored'
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        return 'unscored'


class Reliability:
    """Retained request outcomes, exclusions and recovery state for model selection."""
    def __init__(self, db):
        """Bind retained model outcomes and initialize active recovery-check tracking."""
        self.db = db = adopt(db)
        self.recovering = set()
        migrate(db.database, db, "loadcoach.reliability", RELIABILITY)

    def state(self, key):
        """Read the exclusion epoch, failure streak and cooldown for a model key."""
        row = self.db.execute('SELECT epoch, streak, blocked_until FROM reliability_state WHERE model_id=? AND fingerprint=? AND role=?', key).fetchone()
        return dict(zip(('epoch', 'streak', 'blocked_until'), row or (0, 0, 0)))

    def record(self, request, user, model, key, epoch, outcome, recovery=False, now=None, owner_id=None):
        """Retain one deduplicated request outcome and update exclusion state."""
        if not request or not key[0] or not key[1]:
            return
        now = time.time() if now is None else now
        with self.db:
            state = self.state(key)
            if epoch != state['epoch']:
                return  # an operator reset while this request was running
            inserted = self.db.execute('INSERT INTO reliability_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT(request) DO NOTHING',
                (request, now, user, owner_id or user, model, *key, epoch, outcome, int(recovery))).rowcount
            if not inserted:
                return
            streak, blocked = state['streak'], state['blocked_until']
            if outcome in FAULTS:
                streak += 1
                if streak >= 3 or recovery:
                    blocked = now + COOLDOWN
            elif outcome == 'success':
                streak = 0
                if recovery:
                    blocked = 0
            self.db.execute('INSERT INTO reliability_state VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(model_id,fingerprint,role) DO UPDATE SET epoch=excluded.epoch,streak=excluded.streak,blocked_until=excluded.blocked_until', (*key, epoch, streak, blocked))

    def _scored(self, key, epoch, now, owner, limit=100):
        """Read the same ordered scoring window for aggregates and request drilldowns."""
        return self.db.execute('''SELECT outcome, feedback, recovery, request, at FROM reliability_outcomes
            WHERE model_id=? AND fingerprint=? AND role=? AND epoch=? AND at>=?
            AND (CAST(? AS TEXT) IS NULL OR owner_id=?) AND outcome IN ('success', 'error', 'timeout', 'empty')
            ORDER BY at DESC, request DESC LIMIT ?''',
            (*key, epoch, now-WINDOW, owner, owner, limit)).fetchall()

    def issue_requests(self, key, epoch, now=None, owner=None):
        """Request metadata for faults counted in the caller's latest 50 scored outcomes."""
        now = time.time() if now is None else now
        return [dict(request=row[3], at=row[4], outcome=row[0], recovery=bool(row[2]))
                for row in self._scored(key, epoch, now, owner, limit=50) if row[0] in FAULTS]

    def summary(self, key, now=None, owner=None):
        """Summarize recent outcomes and the model's current exclusion status."""
        now = time.time() if now is None else now
        state = self.state(key)
        rows = self._scored(key, state['epoch'], now, owner)
        scored = [row for row in rows if row[0] == 'success' or row[0] in FAULTS]
        recent, previous = scored[:50], scored[50:100]
        counts = Counter(row[0] for row in recent)
        faults = sum(counts[kind] for kind in FAULTS)
        rate = faults / len(recent) if recent else 0
        feedback = [row[1] for row in recent if row[1] is not None and not row[2]]
        factor = min(1.1, max(.5, 1 - .5 * rate + (.1 * sum(feedback) / len(feedback) if feedback else 0)))
        regression = len(recent) >= 10 and len(previous) >= 10 and rate >= sum(row[0] in FAULTS for row in previous)/len(previous) + .2
        blocked = bool(state['blocked_until'])
        label = 'checking recovery' if key in self.recovering else 'cooldown' if blocked and state['blocked_until'] > now else 'recovery required' if blocked else 'eligible'
        reason = f"{faults}/{len(recent)} recent model faults; preference x{factor:.3f}"
        if feedback:
            reason += f"; {len(feedback)} feedback ratings"
        if blocked:
            reason += '; excluded from this role until a successful recovery check or reset'
        return {**state, 'state': label, 'excluded': blocked, 'factor': factor, 'reason': reason,
                'counts': {kind: counts[kind] for kind in ('success', 'error', 'timeout', 'empty')},
                'samples': len(recent), 'success_rate': (1-rate)*100 if recent else None,
                'ignored': dict(self.db.execute('''SELECT outcome, COUNT(*) FROM reliability_outcomes
                    WHERE model_id=? AND fingerprint=? AND role=? AND epoch=? AND at>=?
                    AND (CAST(? AS TEXT) IS NULL OR owner_id=?) AND outcome NOT IN ('success', 'error', 'timeout', 'empty')
                    GROUP BY outcome''', (*key, state['epoch'], now-WINDOW, owner, owner)).fetchall()),
                'positive': sum(value == 1 for value in feedback), 'negative': sum(value == -1 for value in feedback),
                'regression': regression}

    def reset(self, key):
        """Reset a model's reliability exclusion when no recovery check is active."""
        if key in self.recovering:
            raise ValueError('A recovery check is running. Wait for it to finish before resetting.')
        state = self.state(key)
        with self.db:
            self.db.execute('INSERT INTO reliability_state VALUES (?, ?, ?, ?, 0, 0) ON CONFLICT(model_id,fingerprint,role) DO UPDATE SET epoch=excluded.epoch,streak=0,blocked_until=0', (*key, state['epoch']+1))

    def begin_recovery(self, key, now=None):
        """Claim a controlled recovery check for the selected model key."""
        state = self.state(key)
        now = time.time() if now is None else now
        if key in self.recovering or not state['blocked_until'] or state['blocked_until'] > now:
            raise ValueError('Recovery requires an excluded candidate after its five-minute cooldown, with no check already running.')
        self.recovering.add(key)

    def feedback(self, request, owner, value, explanation='', rater=None):
        """Validate and retain a rating for a completed request: the owner's, or a rater's for anyone's when `owner` is None.

        The rating nudges routing while the outcome is within the window, and stays as quality evidence after it.
        """
        if type(value) is not int or value not in (-1, 0, 1):
            raise ValueError('Feedback is -1 (unhelpful), 1 (helpful), or 0 (remove rating).')
        if not isinstance(explanation, str) or len(explanation) > EXPLANATION:
            raise ValueError(f'An explanation is text of at most {EXPLANATION} characters.')
        row = self.db.execute('SELECT owner_id, outcome, recovery, model, model_id, fingerprint, role FROM reliability_outcomes WHERE request=?',
                              (request,)).fetchone()
        if not row or (owner is not None and row[0] != owner):
            raise LookupError('There is no completed scored request of yours with that ID.')
        if row[1] != 'success' or row[2]:
            raise ValueError('Feedback applies to successful ordinary answers only.')
        with self.db:
            self.db.execute('UPDATE reliability_outcomes SET feedback=? WHERE request=?', (value or None, request))
            if value:
                self.db.execute('INSERT INTO quality_ratings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request) DO UPDATE '
                                'SET at=excluded.at, rater=excluded.rater, value=excluded.value, explanation=excluded.explanation',
                                (request, time.time(), rater or row[0], *row[3:], value, explanation.strip()))
            else:
                self.db.execute('DELETE FROM quality_ratings WHERE request=?', (request,))

    def rating(self, request):
        """The rating kept for one request, with its explanation, or None."""
        row = self.db.execute('SELECT value, explanation, rater FROM quality_ratings WHERE request=?', (request,)).fetchone()
        return dict(zip(('value', 'explanation', 'rater'), row)) if row else None

    def quality(self, explained=0):
        """Every rated model fingerprint, all roles together: good and bad counts and the share that were good, newest first.

        With `explained`, each also carries that many of its latest explanations.
        """
        rows = self.db.execute('''SELECT model_id, fingerprint, MAX(model), SUM(CASE WHEN value>0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN value<0 THEN 1 ELSE 0 END), MAX(at) FROM quality_ratings
            GROUP BY model_id, fingerprint ORDER BY MAX(at) DESC''').fetchall()
        found = []
        for mid, fp, model, good, bad, at in rows:
            one = {'model_id': mid, 'fingerprint': fp, 'model': model, 'good': good, 'bad': bad,
                   'score': round(100 * good / (good + bad), 1), 'last_at': at}
            if explained:
                one['explanations'] = [dict(zip(('request', 'at', 'rater', 'role', 'value', 'explanation'), row)) for row in self.db.execute(
                    '''SELECT request, at, rater, role, value, explanation FROM quality_ratings WHERE model_id=? AND fingerprint=?
                    AND explanation<>'' ORDER BY at DESC LIMIT ?''', (mid, fp, explained)).fetchall()]
            found.append(one)
        return found

    def report(self, owner=None, model=None, role=None, now=None):
        """Summarize retained reliability evidence for matching models and roles."""
        now = time.time() if now is None else now
        keys = self.db.execute('''SELECT model_id, fingerprint, role, MAX(model), MAX(at), MAX(request)
            FROM reliability_outcomes WHERE at>=? AND (CAST(? AS TEXT) IS NULL OR owner_id=?)
            AND (CAST(? AS TEXT) IS NULL OR model=?) AND (CAST(? AS TEXT) IS NULL OR role=?)
            GROUP BY model_id, fingerprint, role ORDER BY MAX(at) DESC''',
            (now-WINDOW, owner, owner, model, model, role, role)).fetchall()
        return [{'model_id': mid, 'fingerprint': fp, 'role': work, 'model': name, 'last_at': at, 'last_request': request,
                 **self.summary((mid, fp, work), now, owner)} for mid, fp, work, name, at, request in keys]

    def prune(self, keep_days):
        """Delete outcomes older than the configured reliability retention window."""
        if keep_days:
            with self.db:
                self.db.execute('DELETE FROM reliability_outcomes WHERE at<?', (time.time()-max(7, keep_days)*86400,))
