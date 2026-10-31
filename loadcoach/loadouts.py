"""Named fixed-card/context selections using the model store, setup reviews and scheduler.

Definitions and guarded undo survive restart. Runtime set leases are independent of ordinary
voice/manual holds and expire; this module never creates an inference process or wakes a host.
"""

import asyncio
import copy
import json
import secrets
import time

from . import model_setup
from .models import at_context
from .runner import Refusal, Server

HOLD_SECONDS = 600


class Conflict(ValueError):
    """A saved selection or reviewed model changed before its operation committed."""


class Loadouts:
    """Versioned model configuration sets and temporary residency leases."""
    def __init__(self, library, runner):
        """Bind saved configuration sets to their model library and admission runner."""
        self.library, self.runner, self.db = library, runner, library.db

    def get(self, name):
        """Read one versioned load-out and its saved undo receipt."""
        row = self.db.execute('SELECT version,spec,updated,undo FROM model_loadouts WHERE name=?', (name,)).fetchone()
        if row is None:
            raise LookupError('That load-out does not exist.')
        return {'name': name, 'version': row[0], 'models': json.loads(row[1]), 'updated': row[2],
                'undo': json.loads(row[3]) if row[3] else None}

    def listed(self):
        """List retained load-outs in name order."""
        found = []
        for name, in self.db.execute('SELECT name FROM model_loadouts ORDER BY name'):
            saved = self.get(name)
            saved['models'] = [{**one, 'name': self.library.name_for(one['model_id'])} for one in saved['models']]
            found.append(saved)
        return found

    def save(self, name, models, version=None):
        """Validate and persist a load-out using optimistic version checks."""
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('A load-out name is 1–80 characters without control characters.')
        if not isinstance(models, list) or not 1 <= len(models) <= 16:
            raise ValueError('Choose one to sixteen models.')
        selected, identities = [], set()
        for one in models:
            if not isinstance(one, dict) or one.keys() != {'model', 'cards', 'context'}:
                raise ValueError('Each selection contains model, cards and context.')
            model, cards, context = one['model'], one['cards'], one['context']
            mid = self.library.identity(model) if isinstance(model, str) else None
            if not mid or mid in identities:
                raise ValueError('Choose existing local models, each once.')
            if not isinstance(cards, list) or not all(isinstance(card, str) for card in cards) or len(set(cards)) != len(cards) or not set(cards) <= set(self.runner.config.gpus):
                raise ValueError('Cards are a list of distinct configured GPU names; an empty list explicitly selects CPU.')
            if type(context) is not int or not 1 <= context < 2**31:
                raise ValueError('Context is a positive 32-bit whole number.')
            selected.append({'model_id': mid, 'label': model, 'cards': [card for card in self.runner.config.gpus if card in cards], 'context': context})
            identities.add(mid)
        name = name.strip()
        with self.db:
            exists = self.db.execute('SELECT version FROM model_loadouts WHERE name=?', (name,)).fetchone()
            if exists:
                if type(version) is not int or version != exists[0] or version >= 2**31-1:
                    raise Conflict('The saved load-out changed. Review its current version before replacing it.')
                changed = self.db.execute('UPDATE model_loadouts SET version=version+1,spec=?,updated=? WHERE name=? AND version=?',
                    (json.dumps(selected), time.time(), name, version))
                if not changed.rowcount:
                    raise Conflict('The saved load-out changed. Reload it.')
            else:
                if version is not None or self.db.execute('SELECT COUNT(*) FROM model_loadouts').fetchone()[0] >= 64:
                    raise Conflict('The load-out was removed, or the limit of 64 saved sets was reached.')
                added = self.db.execute('INSERT INTO model_loadouts VALUES (?,1,?,?,NULL) ON CONFLICT(name) DO NOTHING',
                    (name, json.dumps(selected), time.time()))
                if not added.rowcount:
                    raise Conflict('That load-out was saved concurrently. Reload it.')
        return self.get(name)

    def delete(self, name, version):
        """Delete a load-out only when its version still matches the reviewed one."""
        with self.db:
            if type(version) is not int or not self.db.execute('DELETE FROM model_loadouts WHERE name=? AND version=?', (name, version)).rowcount:
                raise Conflict('The load-out changed or was removed. Reload it before deleting.')

    def configuration(self, one):
        """Resolve a load-out entry to current model settings by stable identity."""
        name = self.library.name_for(one['model_id'])
        if name is None:
            raise ValueError(f"{one['label']} was removed; recreating its name cannot replace its stable model ID.")
        current = self.library.setup(name)
        settings = at_context(current['settings'], one['context'])
        settings = {**settings, 'gpus': one['cards'] or 0}
        if 'min_context' in settings:
            settings['min_context'] = one['context']
        if current['settings'].get('gpus') == 'any':
            # Preserve automatic placement's explicit full offload and one-card split rules.
            if not settings['flags'].keys() & {'gpu-layers', 'n-gpu-layers'}:
                settings['flags']['gpu-layers'] = 'all'
            if len(one['cards']) <= 1:
                settings['flags'].pop('tensor-split', None)
        return name, {'settings': settings, 'order': current['order']}

    def joint_fit(self, reviews, lock=''):
        """Run the real placement rule on a disposable scheduler snapshot, pinning each pick.

        Existing Server objects are read only. The snapshot owns its maps, holds, wake event
        and fake ready servers, so neither rejection nor preview can unload or pin anything.
        """
        trial = copy.copy(self.runner)
        trial.servers = dict(self.runner.servers)
        trial.holds = dict(self.runner.holds)
        trial.loadout_leases = dict(self.runner.loadout_leases)
        trial.seen = copy.deepcopy(self.runner.seen)
        trial.wake, trial.note = asyncio.Event(), lambda *_, **__: None
        placements, leaves, errors = [], [], []
        held = trial.holder()
        if held and held.token != lock:
            errors.append('Another GPU lock is active.')
        for review in reviews:
            if review['errors']:
                continue
            name, settings = review['name'], review['effective']
            server = trial.serving(name, settings)
            reusable = server and server.ready and not server.dead() and trial.answers(server, settings)
            fitted = None if reusable else trial.fit(name, settings, set(), configured=settings)
            if not reusable and fitted is None:
                errors.append(f'{name} cannot fit now without displacing protected work or another member. Unmeasured placements reserve whole cards.')
                continue
            if reusable:
                cards, launch = server.cards, server.settings
            else:
                launch, cards, gone = fitted
                for old in [*trial.replacements(name, settings), *gone]:
                    trial.servers.pop(old.model, None)
                    # A process removed only from the snapshot must not reappear as an
                    # outside GPU user when the next member's placement is calculated.
                    if trial.seen and old.process:
                        used = trial.seen['pids'].pop(old.process.pid, {})
                        for card, mib in used.items():
                            if card in trial.seen['cards']:
                                trial.seen['cards'][card]['used'] = max(0, trial.seen['cards'][card]['used'] - mib)
                    if old.model not in leaves:
                        leaves.append(old.model)
                measure = self.library.measure(name, launch)
                server = Server(name, cards, launch, configured=settings, ready=True,
                                mib=measure['mib'] if measure and len(measure['mib']) == len(cards) else None)
                trial.servers[name] = server
            if launch['flags']['ctx-size'] != settings['flags']['ctx-size'] or set(cards) != set(settings.get('gpus') or []):
                errors.append(f'{name} cannot be admitted at its exact saved cards and context.')
            trial.holds[name, 'preview'] = float('inf')
            placements.append({'name': name, 'cards': list(cards), 'context': launch['flags']['ctx-size'], 'reuse': bool(reusable),
                               'measured': bool(self.library.measure(name, launch))})
        return {'fits': bool(reviews) and not errors and len(placements) == len(reviews), 'errors': errors,
                'placements': placements, 'leaves': leaves, 'note': 'Snapshot only; ordinary admission rechecks live resources.'}

    def preview(self, name, supported, engine_error='', undo=False, lock=''):
        """Validate every model in a load-out and assemble the proposed changes."""
        saved = self.get(name)
        errors, reviews = [], []
        if undo:
            if not saved['undo']:
                raise Conflict('There is no current undo for this load-out.')
            sources = saved['undo']['models']
        else:
            sources = saved['models']
        for one in sources:
            try:
                if undo:
                    model = self.library.name_for(one['model_id'])
                    previous = self.library.setup_undo(model) if model else None
                    if not previous or previous['receipt'] != one['receipt'] or previous['after'] != self.library.setup(model):
                        raise Conflict('A member changed; this load-out undo cannot overwrite it.')
                    configuration = previous['before']
                else:
                    model, configuration = self.configuration(one)
                review = model_setup.preview(model, configuration, self.library, self.runner, supported, engine_error)
                reviews.append({**review, 'name': model, 'undo': one['receipt'] if undo else ''})
                errors.extend(f'{model}: {message}' for message in review['errors'])
            except ValueError as problem:
                errors.append(str(problem))
        joint = ({'fits': False, 'errors': [], 'placements': [], 'leaves': [],
                  'note': 'Undo restores configuration only; it does not load or stop models.'} if undo else self.joint_fit(reviews, lock))
        token = model_setup.digest([name, saved['version'], saved['models'], saved['undo'] if undo else None,
                                    [one['token'] for one in reviews], errors, bool(undo)])
        return {'name': name, 'version': saved['version'], 'models': reviews, 'errors': errors, 'joint': joint,
                'undo': bool(undo), 'token': token, 'can_apply': not errors and bool(reviews),
                'can_request': not undo and not errors and joint['fits'], 'hold_seconds': HOLD_SECONDS}

    def apply(self, review):
        """Apply a validated configuration set and retain its undo receipt."""
        if not review['can_apply']:
            raise ValueError('Resolve every static validation error before applying the set.')
        changed = [one for one in review['models'] if one['before'] != one['candidate']]
        if not changed:
            return None
        with self.db:
            current = self.get(review['name'])
            if current['version'] != review['version']:
                raise Conflict('The load-out changed. Preview it again.')
            for one in sorted(changed, key=lambda one: one['model_id']):
                self.library.write_setup(one['name'], one['candidate'], one['before'], one['undo'],
                    identity=one['model_id'], revision=one['file_revision'])
            receipt = None if review['undo'] else {'receipt': secrets.token_hex(16), 'models': [
                {'model_id': one['model_id'], 'receipt': self.library.setup_undo(one['name'])['receipt']} for one in changed]}
            updated = self.db.execute('UPDATE model_loadouts SET undo=? WHERE name=? AND version=?',
                (json.dumps(receipt) if receipt else None, review['name'], review['version']))
            if not updated.rowcount:
                raise Conflict('The load-out changed. Preview it again.')
        return receipt

    def lease(self, name, user, owner_id, models, *, state='ready', version=None):
        """Create a bounded temporary residency lease for the selected load-out."""
        self.runner.held()
        if len(self.runner.loadout_leases) >= 64:
            raise ValueError('There are already 64 active load-out leases.')
        identity = secrets.token_hex(16)
        self.runner.loadout_leases[identity] = {'name': name, 'user': user, 'owner_id': owner_id,
            'models': models, 'state': state, 'version': version, 'until': time.monotonic() + HOLD_SECONDS,
            '_servers': {mid: server for mid in models if (model_name := self.library.name_for(mid)) and (server := self.runner.serving(model_name))}}
        asyncio.get_running_loop().call_later(HOLD_SECONDS, self.runner.wake.set)
        return identity

    def release(self, identity, owner_id):
        """Release a load-out lease owned by the current caller."""
        self.runner.held()
        lease = self.runner.loadout_leases.get(identity)
        if not lease or lease['owner_id'] != owner_id:
            raise LookupError('That load-out lease is not yours or has expired.')
        del self.runner.loadout_leases[identity]
        self.runner.wake.set()
        return lease

    def renew(self, identity, owner_id):
        """Extend a still-authorized load-out residency lease."""
        self.runner.held()
        lease = self.runner.loadout_leases.get(identity)
        if not lease or lease['owner_id'] != owner_id or lease['state'] != 'ready':
            raise LookupError('That ready load-out lease is not yours or has expired.')
        for mid, configuration in lease['models'].items():
            name = self.library.name_for(mid)
            server = self.runner.serving(name) if name else None
            if not name or self.library.stored(name) != configuration['settings'] or not server or server is not lease['_servers'].get(mid) or not server.ready or server.dead() or not self.runner.answers(server, self.library.get(name)):
                raise Conflict('A leased model changed or is no longer ready. Release and preview the load-out again.')
        lease['until'] = time.monotonic() + HOLD_SECONDS
        asyncio.get_running_loop().call_later(HOLD_SECONDS, self.runner.wake.set)
        return lease

    async def request(self, review, user, owner_id, acquire):
        """Apply one reviewed set atomically, then await ordinary admission for every member.

        Native load failures leave the complete applied configuration and its guarded undo.
        New set holds are released on failure/cancellation; independent holds and active
        requests remain untouched. We never claim a partly loaded set is ready.
        """
        if not review['can_request']:
            raise ValueError('The whole set must pass static validation and joint fit before loading.')
        identity = self.lease(review['name'], user, owner_id, {}, state='preparing', version=review['version'])
        lease = self.runner.loadout_leases[identity]
        lease['until'] += self.runner.config.max_wait
        ready = False
        try:
            self.apply(review)
            async with asyncio.timeout(self.runner.config.max_wait):
                for one in review['models']:
                    name = self.library.name_for(one['model_id'])
                    if not name or self.library.stored(name) != one['candidate']['settings']:
                        raise Conflict('A model changed during loading. Preview the whole set again.')
                    if identity not in self.runner.loadout_leases:
                        raise Conflict('This load-out lease was released while preparing.')
                    ticket = await acquire(name, self.library.get(name))
                    try:
                        server = ticket.server
                        if (not server or not server.ready or server.dead() or self.library.identity(name) != one['model_id'] or
                            self.library.stored(name) != one['candidate']['settings'] or
                            not self.runner.answers(server, self.library.get(name)) or
                            server.settings['flags']['ctx-size'] != one['candidate']['settings']['flags']['ctx-size']):
                            raise Conflict('A model did not become ready at its reviewed configuration.')
                        lease['models'][one['model_id']] = one['candidate']
                        lease['_servers'][one['model_id']] = server
                    finally:
                        self.runner.release(ticket)
            lease['state'] = 'ready'
            self.renew(identity, owner_id)
            ready = True
            return identity
        except TimeoutError:
            raise Refusal(503, 'The load-out did not become ready before max_wait.', 10) from None
        finally:
            if not ready:
                self.runner.loadout_leases.pop(identity, None)
                self.runner.wake.set()
