"""Classifier metadata, bounded native requests and the JSON Schema chat adapter."""

import json
import math
import secrets
import time
from functools import lru_cache
from pathlib import Path

MAX_BODY = 65536
NATIVE_PATHS = ('/v1/systemone', '/v1/classifier')
UNCALIBRATED = 'Probabilities use the model’s stored temperatures and are not calibrated for your data.'


class Invalid(ValueError):
    """A public request error with an HTTP status."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=256)
def _file_facts(path, stamp):
    from engines.llama_cpp.gguf import facts, read_metadata
    return facts(read_metadata(path))


def model_facts(settings):
    """Read only the header, invalidating cached facts when its file changes."""
    if not settings or settings.get('engine', 'llama_cpp') != 'llama_cpp':
        return {}
    path = settings.get('flags', {}).get('model')
    try:
        stat = Path(path).stat()
        facts = _file_facts(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return facts if facts.get('what') == 'classifier' else {}
    except (OSError, TypeError, ValueError):
        return {}


def limits(facts):
    return {'questions': 64, 'choices': 52 if facts.get('decision_type') == 'openjev' else 255,
            'score_levels': 10, 'body_bytes': MAX_BODY,
            'max_head_tokens': facts.get('decision_max_head_tokens'),
            'whole_prompt_batch': facts.get('decision_type') in ('laya', 'clef')}


def _payload(value):
    return isinstance(value, (str, dict, list))


def validate(body, choice_limit=255):
    """Normalize chat state and reject malformed work before model admission."""
    if not isinstance(body, dict) or body.keys() - {'model', 'state', 'messages', 'questions', 'files', 'images', 'node'}:
        raise Invalid('Use model, state (or messages), questions and optional images/files and node.')
    if not isinstance(body.get('model'), str) or not body['model'].strip():
        raise Invalid('Choose a classifier model.')
    if 'messages' in body and 'state' in body:
        raise Invalid('Supply state or messages, not both.')
    sent = dict(body)
    if 'messages' in sent:
        messages = sent.pop('messages')
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or
                m.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool') or
                not isinstance(m.get('content'), (str, list)) for m in messages):
            raise Invalid('messages must be a nonempty chat message array.')
        sent['state'] = {'messages': messages}
    if not _payload(sent.get('state')):
        raise Invalid('state must be text, a JSON object or an array.')
    questions = sent.get('questions')
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 64:
        raise Invalid('questions must be an object containing 1–64 questions.')
    for key, question in questions.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not isinstance(question, dict):
            raise Invalid('Each question needs an ID of 1–128 characters and an object.')
        if question.keys() - {'type', 'instructions', 'criteria'} or not _payload(question.get('instructions')):
            raise Invalid(f'{key}: supply type, instructions and optional criteria.')
        kind, criteria = question.get('type'), question.get('criteria')
        if kind == 'choice':
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= choice_limit:
                raise Invalid(f'{key}: choice criteria needs 2–{choice_limit} options.')
            if any(not isinstance(label, str) or not label or (description is not None and not _payload(description))
                   for label, description in criteria.items()):
                raise Invalid(f'{key}: option labels must be nonempty; descriptions are text, JSON or null.')
        elif kind == 'score':
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or any(not _payload(v) for v in criteria):
                raise Invalid(f'{key}: score criteria needs 2–10 ordered level descriptions.')
        elif kind == 'noul':
            if criteria is not None and (not isinstance(criteria, dict) or criteria.keys() - {'true', 'false'} or
                    any(v is not None and not _payload(v) for v in criteria.values())):
                raise Invalid(f'{key}: noul criteria may describe true and false only.')
        else:
            raise Invalid(f'{key}: type must be choice, score or noul.')
    for field in ('images', 'files'):
        if field in sent and (not isinstance(sent[field], list) or len(sent[field]) > 16 or
                any(not isinstance(v, str) or not v.startswith('data:image/') or ';base64,' not in v for v in sent[field])):
            raise Invalid(f'{field} accepts at most 16 inline image data URLs.')
    if len(json.dumps(sent, ensure_ascii=False).encode()) > MAX_BODY:
        raise Invalid('Classifier request exceeds the 64 KiB body limit.')
    return sent


def shim_request(body):
    """Map the supported flat typed JSON Schema to independent questions."""
    hint = 'Classifiers require response_format.json_schema with enum, boolean or bounded integer properties; use /v1/systemone for native questions.'
    if not isinstance(body, dict) or body.keys() - {'model', 'messages', 'response_format', 'stream', 'node', 'logprobs', 'top_logprobs'}:
        raise Invalid(hint)
    if type(body.get('stream', False)) is not bool:
        raise Invalid('stream must be a boolean.')
    format_ = body.get('response_format')
    spec = format_.get('json_schema') if isinstance(format_, dict) and format_.get('type') == 'json_schema' else None
    schema = spec.get('schema') if isinstance(spec, dict) else None
    if not isinstance(schema, dict) or schema.get('type') != 'object' or not isinstance(schema.get('properties'), dict):
        raise Invalid(hint)
    if schema.keys() - {'type', 'properties', 'required', 'additionalProperties', 'description', 'title'}:
        raise Invalid(hint)
    questions, mappings = {}, {}
    for key, prop in schema['properties'].items():
        if not isinstance(prop, dict) or prop.keys() - {'type', 'enum', 'description', 'minimum', 'maximum', 'x-levels', 'title'}:
            raise Invalid(hint)
        q = {'instructions': prop.get('description', key)}
        if prop.get('type') == 'boolean':
            q['type'] = 'noul'
        elif isinstance(prop.get('enum'), list) and prop.get('type', 'string') == 'string' and all(isinstance(v, str) for v in prop['enum']):
            if len(set(prop['enum'])) != len(prop['enum']):
                raise Invalid('enum choices must be unique.')
            q.update(type='choice', criteria=dict.fromkeys(prop['enum']))
        elif prop.get('type') == 'integer':
            low, high = prop.get('minimum'), prop.get('maximum')
            levels = prop.get('x-levels')
            if levels is not None:
                if not isinstance(levels, list) or not 2 <= len(levels) <= 10 or any(not isinstance(v, str) for v in levels):
                    raise Invalid('x-levels must contain 2–10 descriptions.')
                low = 0 if low is None else low
                high = low + len(levels) - 1 if type(low) is int else None
            if type(low) is not int or type(high) is not int or not 2 <= high - low + 1 <= 10:
                raise Invalid(hint)
            q.update(type='score', criteria=levels if levels is not None else [str(v) for v in range(low, high + 1)])
            mappings[key] = low
        else:
            raise Invalid(hint)
        questions[key] = q
    sent = validate({'model': body.get('model'), 'messages': body.get('messages'), 'questions': questions})
    return sent, mappings


def shim_response(native, body, mappings):
    """Return schema-valid values and distributions without claiming generated tokens."""
    values, content = {}, []
    for key, question in body['questions'].items():
        answer = native['answers'][key]
        kind = question['type']
        if kind == 'noul':
            probability = answer['noul']
            values[key] = probability >= .5
            probs = {'false': 1 - probability, 'true': probability}
        elif kind == 'score':
            low = mappings[key]
            score = float(answer['score'])
            values[key] = low + min(len(question['criteria']) - 1, max(0, math.floor(score + .5)))
            probs = {str(int(index) + low): p for index, p in answer['probabilities'].items()}
        else:
            values[key] = answer['choice']
            probs = answer['probabilities']
        token = json.dumps(values[key], ensure_ascii=False)
        distribution = [{'token': label, 'logprob': math.log(max(float(p), 1e-300)), 'bytes': list(label.encode())}
                        for label, p in sorted(probs.items(), key=lambda item: -item[1])]
        content.append({'token': token, 'logprob': distribution[0]['logprob'], 'bytes': list(token.encode()),
                        'top_logprobs': distribution, 'question_id': key})
    count = native.get('usage', {}).get('input_tokens', 0)
    return {'id': 'chatcmpl-' + secrets.token_hex(12), 'object': 'chat.completion', 'created': int(time.time()),
            'model': body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(values)},
            'logprobs': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': count, 'completion_tokens': 0, 'total_tokens': count}}


async def adapt_response(response, body, mappings, streaming=False):
    """Consume native output through its normal cleanup and accounting generator."""
    from starlette.responses import JSONResponse, Response
    if response.status_code >= 400:
        return response
    raw = b''.join([chunk async for chunk in response.body_iterator]) if hasattr(response, 'body_iterator') else response.body
    headers = {k: v for k, v in response.headers.items() if k not in ('content-length', 'content-type', 'content-encoding')}
    try:
        result = shim_response(json.loads(raw), body, mappings)
    except (ValueError, TypeError, KeyError, OverflowError):
        return JSONResponse({'error': {'message': 'Invalid classifier response from the inference server.'}}, 502, headers=headers)
    if streaming:
        result['object'] = 'chat.completion.chunk'
        result['choices'][0]['delta'] = result['choices'][0].pop('message')
        return Response('data: ' + json.dumps(result) + '\n\ndata: [DONE]\n\n', media_type='text/event-stream', headers=headers)
    return JSONResponse(result, headers=headers)
