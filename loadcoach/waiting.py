"""Shared advisory wait/completion estimates: measured rates, inferred work, no promises.

Queues are conservatively serialized across potentially overlapping devices. We never count
elapsed wall time as completed tokens, or turn an unknown rate/placement into a zero wait.
"""

import json
import math


def positive(value):
    """Return a finite positive numeric estimate, or None when unavailable."""
    return type(value) in (int, float) and 0 < value < 1e12 and math.isfinite(value)


def workload(asked):
    """Keep counts only, never messages, tools, images or credentials on scheduler tickets."""
    prompt = asked.get('messages', asked.get('prompt', asked.get('input')))
    image = any(isinstance(part, dict) and part.get('type') != 'text'
                for message in prompt if isinstance(message, dict) and isinstance(message.get('content'), list)
                for part in message['content']) if isinstance(prompt, list) else False
    # This is the same rough byte/token ratio used for role context checks, not a tokenizer.
    tokens = None if image or prompt is None else max(1, len(json.dumps([prompt, asked.get('tools')], ensure_ascii=False).encode()) // 3)
    output = asked.get('max_completion_tokens', asked.get('max_tokens'))
    output = output if type(output) is int and 0 < output <= 2**31-1 else None
    choices = asked.get('n', 1)
    candidates = asked.get('best_of', choices)
    if type(choices) is not int or type(candidates) is not int or not 1 <= choices <= candidates <= 2**31-1:
        output = None
    elif output is not None:
        output *= candidates
    if 'input' in asked and 'messages' not in asked and 'prompt' not in asked:
        output = 0  # embeddings do not generate completion tokens
    return {'prompt_tokens': tokens, 'output_tokens': output, 'basis': 'inferred prompt bytes / 3 and requested maximum output'}


def duration(work, speed, prompt_speed):
    """Budget processing time; a missing required rate or unbounded output stays unknown."""
    if not work or work.get('prompt_tokens') is None or work.get('output_tokens') is None:
        return None
    if work['prompt_tokens'] and not positive(prompt_speed) or work['output_tokens'] and not positive(speed):
        return None
    seconds = (work['prompt_tokens'] / prompt_speed if work['prompt_tokens'] else 0) + (work['output_tokens'] / speed if work['output_tokens'] else 0)
    return seconds if math.isfinite(seconds) and seconds < 1e12 else None


def completion(wait, work, speed, prompt_speed):
    """Estimate request duration from retained compatible completion measurements."""
    own = duration(work, speed, prompt_speed)
    total = wait.get('seconds') + own if wait and wait.get('seconds') is not None and own is not None else None
    return {'seconds': round(total, 3) if total is not None else None,
            'wait_seconds': wait.get('seconds') if wait else None,
            'work_seconds': round(own, 3) if own is not None else None,
            'work': work, 'basis': 'measured rates with inferred token budgets; advisory snapshot'}


def estimate(runner, ticket, paces):
    """Conservative resource-aware snapshot before admission. The real scheduler still decides."""
    from .runner import RANK

    settings = runner.models.get(ticket.model) or ticket.settings
    total, load, ahead, unknown = 0., 0., 0, []
    counted_loads = set()

    def loading(name):
        """Estimate model-loading duration from compatible retained measurements."""
        nonlocal load
        if name in counted_loads:
            return
        counted_loads.add(name)
        value = paces.get('load', {}).get(name)
        if type(value) not in (int, float) or not 0 <= value < 1e12 or not math.isfinite(value):
            unknown.append('No measured load time is available.')
        else:
            load += value

    def devices(config, server=None):
        """Resolve the devices required by a candidate placement."""
        if server and runner.answers(server, config):
            return set(server.cards)
        need = runner.needs(config)
        return set(need) if isinstance(need, list) else set(runner.cards) if need else set()

    def overlaps(left, right):
        """Return whether two requests compete for any of the same devices."""
        return bool(left & right) or not left and not right  # CPU work is conservatively serialized too

    if settings is None:
        unknown.append('The model configuration is unavailable.')
        cards, server = set(), None
    else:
        server = runner.serving(ticket.model, settings)
        cards = devices(settings, server)
    lock = runner.holder()
    if lock and ticket.token != lock.token:
        unknown.append('Another lock can be renewed; its release time is unknown.')
    queued = [one for one in runner.waiting if one is not ticket and RANK[one.priority] <= RANK[ticket.priority]]
    relevant = []
    for one in [*runner.running, *queued]:
        config = runner.models.get(one.model) or one.settings if one.model else None
        if config is None:
            unknown.append('A control request ahead has an unknown completion time.')
        elif overlaps(cards, devices(config, one.server)):
            relevant.append(one)
    ready = bool(server and server.ready and not server.dead() and runner.answers(server, settings))
    # Existing parallel capacity can admit immediately unless earlier queued work needs it.
    if ready and runner.busy(server) < runner.room_for(server.settings) and not any(one in queued for one in relevant) and not unknown:
        return {'seconds': 0., 'queue_seconds': 0., 'load_seconds': 0., 'ahead_requests': 0,
                'unknown': [], 'basis': 'ready parallel capacity; scheduler admission remains authoritative'}
    for one in relevant:
        ahead += 1
        value = duration(one.work, paces.get('speed', {}).get(one.model), paces.get('prompt_speed', {}).get(one.model))
        if value is None:
            unknown.append('Work ahead lacks a measured rate or bounded token budget.')
        else:
            total += value
        if one in queued:
            serving = runner.serving(one.model)
            if not serving or not serving.ready or not runner.answers(serving, runner.models.get(one.model) or one.settings):
                loading(one.model)
    for other in runner.servers.values():
        if not other.ready and overlaps(cards, set(other.cards)):
            loading(other.model)
    if settings and not ready:
        loading(ticket.model)
        placed = runner.fit(ticket.model, settings, set())
        if placed is None:
            protected = any(runner.pinned(other) and overlaps(cards, set(other.cards)) for other in runner.servers.values())
            outside = any(runner.known(card) and runner.others(card) for card in cards)
            measured = runner.models.measure(ticket.model, settings)
            if protected or outside or not relevant or (cards and not measured):
                unknown.append('Future GPU placement is unknown or protected; no capacity is promised.')
            else:
                total += runner.config.model_idle_minutes * 60
    seconds = total + load
    if not math.isfinite(seconds) or seconds >= 1e12:
        unknown.append('The estimate exceeds the supported range.')
    return {'seconds': round(seconds, 3) if not unknown else None, 'queue_seconds': round(total, 3),
            'load_seconds': round(load, 3), 'ahead_requests': ahead, 'unknown': sorted(set(unknown)),
            'basis': 'measured rates; inferred token budgets; potentially overlapping work serialized without elapsed-time discounts'}


def headers(value):
    """Render available wait estimates into the public response headers."""
    return {'x-loadcoach-estimated-wait': str(value['seconds']) if value['seconds'] is not None else 'unknown',
            'x-loadcoach-estimate-basis': 'measured-rates/inferred-work; advisory'}
