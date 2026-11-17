"""Run an image workflow graph: map a caller's inputs, order the steps and send each one as an ordinary image request.

Images, not latents, pass between steps (docs/advanced-workflow-plan.md). Each Sampler or Upscale
step is one request through the existing image API, so admission, routing, logging and usage
apply per step. Resize and Combine run here. Intermediate images stay in this run's memory;
Output and Compare images are returned (and saved by the caller of `run`).
"""

import asyncio
import base64
import binascii
import io
import math
import re
import secrets

from PIL import Image

from mirrorwall import graph

# The fields an image API caller may send with a workflow:<name> model.
CALLER_FIELDS = {'model', 'prompt', 'negative_prompt', 'size', 'seed', 'n', 'image', 'workflow_inputs', 'stream', 'user',
                 'response_format', 'output_format', 'quality', 'background', 'moderation'}
MAX_ITEMS = 16
ASSET_ID = re.compile(r'image_[0-9a-f]{32}')
WORKFLOW_MODEL = re.compile(r'workflow:([a-z0-9][a-z0-9._-]{0,79})(?:@([1-9][0-9]{0,8}))?')


class RunError(ValueError):
    """A run cannot start or a step failed; the message is safe to show the caller."""


def parse_model(value):
    """`workflow:<api name>[@version]` as (api name, version or None), or None for an ordinary model name."""
    if not isinstance(value, str) or not value.startswith('workflow:'):
        return None
    match = WORKFLOW_MODEL.fullmatch(value)
    if not match:
        raise RunError('Name a workflow as workflow:<name> or workflow:<name>@<version>.')
    return match.group(1), int(match.group(2)) if match.group(2) else None


def _first(document, kind, field):
    return next((node for node in document['nodes'] if node['type'] == kind and field in node['exposed']), None)


def prepare(document, asked):
    """Apply a caller's inputs to a copy of a checked document. Returns (document, run item count, image inputs).

    Standard fields map to the first exposed input of their kind; `workflow_inputs` names any other
    exposed input (`step.field`, or a Load image step's label) and overridable fields within their
    bounds. Anything the workflow does not expose is refused, never ignored.
    """
    if not isinstance(asked, dict):
        raise RunError('Send a JSON object.')
    unknown = asked.keys() - CALLER_FIELDS
    if unknown:
        raise RunError('This workflow takes prompt, negative_prompt, size, seed, n, image and workflow_inputs; '
                       'it does not take ' + ', '.join(sorted(unknown)) + '. Set exposed inputs with workflow_inputs.')
    for key, permitted in (('response_format', ('b64_json',)), ('output_format', ('png',)), ('quality', ('auto',)),
                           ('background', ('auto', 'opaque')), ('moderation', ('auto',))):
        if key in asked and asked[key] not in permitted:
            raise RunError(f'Unsupported {key} for a workflow.')
    if type(asked.get('stream', False)) is not bool:
        raise RunError('stream must be true or false.')
    document = {**document, 'nodes': [{**node, 'values': dict(node['values'])} for node in document['nodes']]}
    nodes = {node['id']: node for node in document['nodes']}
    fields = {node['id']: {field['name']: field for field in graph.COMPONENTS[node['type']].get('fields', [])} for node in document['nodes']}
    images = {}

    def put(node, name, value):
        field = fields[node['id']][name]
        try:
            node['values'][name] = graph._value(field, value)
        except ValueError as problem:
            raise RunError(f"{node['id']}.{name}: {problem}") from None
        if field['kind'] == 'number' and field.get('step') == 1 and node['values'][name] is not None:
            if node['values'][name] != int(node['values'][name]):
                raise RunError(f"{node['id']}.{name} must be a whole number.")
            node['values'][name] = int(node['values'][name])

    for key, kind, field in (('prompt', 'prompt', 'text'), ('negative_prompt', 'negative', 'text')):
        if key in asked:
            node = _first(document, kind, field)
            if not node:
                raise RunError(f'This workflow does not take {key}.')
            if not isinstance(asked[key], str) or (key == 'prompt' and not asked[key].strip()):
                raise RunError(f'{key} must be text.')
            put(node, field, asked[key])
    if 'seed' in asked:
        node = _first(document, 'seed', 'value')
        if not node:
            raise RunError('This workflow does not take seed.')
        if asked['seed'] != -1:
            put(node, 'value', asked['seed'])
            if node['values']['mode'] == 'random':
                node['values']['mode'] = 'fixed'
    if 'size' in asked:
        node = next((one for one in document['nodes'] if one['type'] == 'size' and {'width', 'height'} <= set(one['exposed'])), None)
        if not node:
            raise RunError('This workflow does not take size.')
        if not isinstance(asked['size'], str) or not re.fullmatch(r'\d{3,4}x\d{3,4}', asked['size']):
            raise RunError('Use an explicit WIDTHxHEIGHT size.')
        width, height = map(int, asked['size'].split('x'))
        put(node, 'width', width)
        put(node, 'height', height)
    if 'image' in asked:
        given = asked['image'] if isinstance(asked['image'], list) else [asked['image']]
        slots = [node for node in document['nodes'] if node['type'] == 'load_image' and 'asset' in node['exposed']]
        if len(given) > len(slots):
            raise RunError(f'This workflow takes {len(slots)} image{"s" if len(slots) != 1 else ""}.' if slots else 'This workflow does not take images.')
        for node, value in zip(slots, given):
            images[node['id']] = value
    extra = asked.get('workflow_inputs', {})
    if not isinstance(extra, dict) or len(extra) > 64:
        raise RunError('workflow_inputs maps exposed inputs and overridable step fields to values.')
    for key, value in extra.items():
        step, _, name = str(key).partition('.')
        node = nodes.get(step)
        if node and node['type'] == 'load_image' and not name and 'asset' in node['exposed']:
            images[step] = value
            continue
        if not node or not name or not (name in node['exposed'] or name in node['overridable']):
            raise RunError(f'{key} is not an input or overridable field of this workflow.')
        put(node, name, value)
        if name in node['overridable']:
            low, high = node['overridable'][name]
            if not low <= node['values'][name] <= high:
                raise RunError(f'{key} must be from {low:g} to {high:g}.')
    count = asked.get('n', 1)
    if type(count) is not int or not 1 <= count <= MAX_ITEMS:
        raise RunError(f'n must be a whole number from 1 to {MAX_ITEMS}.')
    stages = sum(bool(graph.COMPONENTS[node['type']].get('stage')) for node in document['nodes'])
    if stages * count > graph.LIMITS['images']:
        raise RunError(f"A run is limited to {graph.LIMITS['images']} steps in all; lower n.")
    for node in document['nodes']:
        if node['type'] == 'load_image' and node['id'] not in images and node['values']['asset']:
            images[node['id']] = node['values']['asset']
    return document, count, images


def order(document):
    """Run order: every step after its sources, keeping steps on one model together so it stays loaded."""
    into = {}
    for link in document['links']:
        into.setdefault(link['to'][0], set()).add(link['from'][0])
    document_nodes = {node['id']: node for node in document['nodes']}
    def model_of(node):
        if node['type'] not in ('sampler', 'tiled_detail'):
            return None
        source = next((link['from'][0] for link in document['links'] if link['to'] == [node['id'], 'model']), None)
        return document_nodes[source]['values']['model'] if source else None
    done, result, current = set(), [], None
    remaining = [node for node in document['nodes'] if node['type'] != 'note']
    while remaining:
        ready = [node for node in remaining if into.get(node['id'], set()) <= done]
        if not ready:
            raise RunError('The links form a loop.')
        chosen = (next((node for node in ready if not graph.COMPONENTS[node['type']].get('stage')), None)
                  or next((node for node in ready if current and model_of(node) == current), None)
                  or next((node for node in ready if node['type'] not in ('sampler', 'tiled_detail')), None) or ready[0])
        if chosen['type'] in ('sampler', 'tiled_detail'):
            current = model_of(chosen)
        done.add(chosen['id'])
        result.append(chosen)
        remaining.remove(chosen)
    return result


def plan(document):
    """Model loads and requests for one run item, as the run panel shows them."""
    steps = order(document)
    loads, last = 0, None
    nodes = {node['id']: node for node in document['nodes']}
    for node in steps:
        if node['type'] in ('sampler', 'tiled_detail'):
            source = next((link['from'][0] for link in document['links'] if link['to'] == [node['id'], 'model']), None)
            model = nodes[source]['values']['model'] if source else None
            loads += model != last
            last = model
    return {'steps': [node['id'] for node in steps if graph.COMPONENTS[node['type']].get('stage')], 'model_loads': loads}


def decode(value):
    """Base64 (optionally a data URL) to a bounded, verified RGB image."""
    from PIL import Image, ImageOps
    if not isinstance(value, str):
        raise RunError('An image must be base64 text or one of your image asset IDs.')
    if value.startswith('data:'):
        value = value.partition(',')[2]
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise RunError('An image is not valid base64.') from None
    if len(raw) > 48 * 2**20:
        raise RunError('An image exceeds 48 MiB.')
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.width * opened.height > 33554432:
                raise RunError('An image exceeds 32 megapixels.')
            return ImageOps.exif_transpose(opened).convert('RGB')
    except RunError:
        raise
    except Exception:
        raise RunError('An image could not be read; send PNG, JPEG or WebP.') from None


def encode(image):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return base64.b64encode(buffer.getvalue()).decode()


def resized(image, mode, width, height):
    """Plain geometry: fit inside, fill and crop, crop the centre, or fit and pad."""
    from PIL import Image, ImageOps
    size = (int(width), int(height))
    if mode == 'fit':
        scale = min(size[0] / image.width, size[1] / image.height)
        return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    if mode == 'fill':
        return ImageOps.fit(image, size, Image.Resampling.LANCZOS)
    if mode == 'crop':
        left, top = max(0, (image.width - size[0]) // 2), max(0, (image.height - size[1]) // 2)
        return image.crop((left, top, left + min(size[0], image.width), top + min(size[1], image.height)))
    return ImageOps.pad(image, size, Image.Resampling.LANCZOS, color=(0, 0, 0))


def tiles(width, height, tile, overlap, alignment=16):
    """Tile boxes (left, top, right, bottom) covering an image: aligned tiles of at most `tile` pixels a side, neighbours
    sharing at least `overlap` pixels, the last row and column flush with the far edges."""
    def spans(length):
        size = min(int(tile), length // alignment * alignment)
        if size < 256:
            raise RunError('Tiled detail needs an image at least 256 pixels on each side.')
        if size >= length:
            return [(0, size)]
        stride = size - int(overlap)
        starts = list(range(0, length - size, stride)) + [length - size]
        return [(start, start + size) for start in sorted(set(starts))]
    return [(left, top, right, bottom) for top, bottom in spans(height) for left, right in spans(width)]


def blend(base, pieces, overlap):
    """Paste processed tiles in order over `base`; each fades in across `overlap` pixels where it meets tiles already pasted."""
    from PIL import ImageChops
    result = base.copy()
    for (left, top, right, bottom), piece in pieces:
        size = (right - left, bottom - top)
        if piece.size != size:
            piece = piece.resize(size, Image.Resampling.LANCZOS)
        def ramp(length, fade):
            return [min(255, round(255 * (index + 1) / (fade + 1))) if index < fade else 255 for index in range(length)]
        across = Image.new('L', (size[0], 1)); across.putdata(ramp(size[0], int(overlap) if left else 0))
        down = Image.new('L', (1, size[1])); down.putdata(ramp(size[1], int(overlap) if top else 0))
        mask = ImageChops.multiply(across.resize(size), down.resize(size))
        result.paste(piece.convert(result.mode), (left, top), mask)
    return result


def _advanced(values):
    advanced = {}
    for key in ('eta', 'flow_shift', 'img_cfg', 'shifted_timestep', 'clip_skip'):
        if values.get(key) is not None:
            advanced[key] = int(values[key]) if key in ('shifted_timestep', 'clip_skip') else values[key]
    if values.get('vae_tiling'):
        advanced['vae_tiling'] = True
    if values.get('cache_mode', 'off') != 'off':
        advanced['cache_mode'] = values['cache_mode']
    if values.get('hires'):
        if values['hires_upscaler'] not in graph.HIRES_UPSCALERS:
            raise RunError('A hires pass with an upscaler file cannot run yet; choose a latent method or add an Upscale step.')
        advanced['hires'] = {'upscaler': values['hires_upscaler'], 'scale': values['hires_scale'],
                             'steps': int(values['hires_steps']), 'denoising_strength': values['hires_denoise']}
    return advanced


async def run(document, count, images, *, models, upscalers, send, image_of=None):
    """Run every item of a prepared document; yields progress events, then each Output image.

    `send(path, body)` is an async generator of the stage's image events (the caller's own API
    access). `image_of(value)` turns an asset ID into base64. `models` are the caller's image
    models ({'id', 'family'}); `upscalers` the installed files ({'id', 'nodes'}) or None.
    """
    family_of = {model['id']: graph.FAMILIES.get(model.get('family')) for model in models}
    holders = {one['id']: set(one.get('nodes') or []) for one in upscalers or []}
    nodes = {node['id']: node for node in document['nodes']}
    links = document['links']
    def sources(node, port):
        return [link['from'] for link in links if link['to'] == [node['id'], port]]
    steps = order(document)
    loaded = {}
    for step, value in images.items():
        if isinstance(value, str) and ASSET_ID.fullmatch(value):
            if not image_of or not (value := image_of(value)):
                raise RunError(f'{nodes[step]["title"]}: there is no such image of yours.')
        loaded[step] = await asyncio.to_thread(decode, value)
    for node in document['nodes']:
        if node['type'] == 'load_image' and node['id'] not in loaded and any(link['from'][0] == node['id'] for link in links):
            raise RunError(f"{node['title']} needs an image: send image, or workflow_inputs.{node['id']}.")
    base_seed = secrets.randbelow(2**31 - 1)
    total = sum(bool(graph.COMPONENTS[node['type']].get('stage')) for node in steps) * count
    finished = 0
    yield {'type': 'workflow.started', 'items': count, 'steps': total, 'plan': [node['id'] for node in steps if graph.COMPONENTS[node['type']].get('stage')]}
    for item in range(count):
        made, origin, seeds, settings = {}, {}, {}, {}  # (step, port) -> value; image step -> (model, node) and the seed that made it
        def value(node, port):
            found = sources(node, port)
            return made.get(tuple(found[0])) if found else None
        for node in steps:
            kind, values, label = node['type'], node['values'], node['id']
            if kind in ('prompt', 'negative'):
                made[(label, 'text')] = values['text']
            elif kind == 'combine':
                made[(label, 'text')] = values['separator'].join(text for text in (value(node, 'first'), value(node, 'second')) if text)
            elif kind == 'model':
                made[(label, 'model')] = values['model']
            elif kind == 'seed':
                # random: a fresh seed per run, then one more per item; increment: the value plus the item; fixed: the value
                start = base_seed if values['mode'] == 'random' else int(values['value'])
                made[(label, 'seed')] = (start + (0 if values['mode'] == 'fixed' else item)) % (2**31)
            elif kind == 'size':
                made[(label, 'size')] = (int(values['width']), int(values['height']))
            elif kind == 'load_image':
                made[(label, 'image')] = loaded.get(label)
            elif kind == 'control_image':
                source = value(node, 'image')
                made[(label, 'control')] = {'image': source, 'strength': values['strength'], 'edges': values['kind'] == 'edges'}
            elif kind == 'resize':
                source = value(node, 'image')
                made[(label, 'image')] = await asyncio.to_thread(resized, source, values['mode'], values['width'], values['height'])
                origin[label] = origin.get(sources(node, 'image')[0][0])
                seeds[label] = seeds.get(sources(node, 'image')[0][0])
            elif kind == 'tiled_detail':
                finished += 1
                yield {'type': 'workflow.step', 'step': label, 'title': node['title'], 'item': item, 'state': 'started', 'index': finished, 'total': total}
                model = value(node, 'model')
                family = family_of.get(model)
                if family is None:
                    raise RunError(f"{node['title']}: the model {model} is not available to you.")
                if not family['init']:
                    raise RunError(f"{node['title']}: {family['label']} has no image-to-image pass; use an SDXL model.")
                prompt = value(node, 'prompt')
                if not prompt or not prompt.strip():
                    raise RunError(f"{node['title']} needs a prompt.")
                image = value(node, 'image')
                boxes = tiles(image.width, image.height, values['tile'], values['overlap'], family['alignment'])
                if len(boxes) > graph.MAX_TILES:
                    raise RunError(f"{node['title']}: a {image.width}×{image.height} image needs {len(boxes)} tiles of this size; "
                                   f"at most {graph.MAX_TILES} are allowed, so raise the tile size.")
                seed = value(node, 'seed')
                common = {'model': model, 'prompt': prompt, 'n': 1, 'stream': True, 'steps': int(values['steps']),
                          'sampler': values['sampler'], 'scheduler': values['scheduler'], 'cfg_scale': values['cfg_scale'],
                          'strength': values['strength'], 'seed': seed if seed is not None else (base_seed + item) % (2**31)}
                negative = value(node, 'negative')
                if negative and family['negative']:
                    common['negative_prompt'] = negative
                pieces, where = [], None
                for number, box in enumerate(boxes, 1):
                    width, height = box[2] - box[0], box[3] - box[1]
                    piece = await asyncio.to_thread(lambda: encode(image.crop(box)))
                    body = {**common, 'size': f'{width}x{height}', 'references': [piece], 'reference_megapixels': max(.1, width * height / 1_000_000)}
                    result = None
                    async for event in send('/v1/images/edits', body):
                        if event.get('type') == 'error' or 'error' in event:
                            message = (event.get('error') or {}).get('message') if isinstance(event.get('error'), dict) else event.get('error')
                            raise RunError(f"{node['title']}, tile {number} of {len(boxes)}: {message or 'the step failed.'}")
                        if event.get('type') in ('image_generation.completed', 'image_edit.completed'):
                            result = event
                    if not result:
                        raise RunError(f"{node['title']}, tile {number} of {len(boxes)}: the step ended without an image.")
                    pieces.append((box, await asyncio.to_thread(decode, result['b64_json'])))
                    where = result.get('node') or where
                    yield {'type': 'workflow.progress', 'step': label, 'item': item, 'stage': 'tiles', 'tile': number, 'tiles': len(boxes)}
                made[(label, 'image')] = await asyncio.to_thread(blend, image, pieces, values['overlap'])
                settings[label] = {**{key: one for key, one in common.items() if key not in ('stream', 'n')},
                                   'tile': int(values['tile']), 'overlap': int(values['overlap']), 'tiles': len(boxes)}
                origin[label] = (model, where)
                seeds[label] = seeds.get(sources(node, 'image')[0][0])
                yield {'type': 'workflow.step', 'step': label, 'title': node['title'], 'item': item, 'state': 'completed',
                       'index': finished, 'total': total, 'seed': common['seed'], 'node': where, 'tiles': len(boxes),
                       'width': made[(label, 'image')].width, 'height': made[(label, 'image')].height}
            elif kind in ('sampler', 'upscale'):
                finished += 1
                yield {'type': 'workflow.step', 'step': label, 'title': node['title'], 'item': item, 'state': 'started', 'index': finished, 'total': total}
                if kind == 'sampler':
                    model = value(node, 'model')
                    family = family_of.get(model)
                    if family is None:
                        raise RunError(f"{node['title']}: the model {model} is not available to you.")
                    prompt = value(node, 'prompt')
                    if not prompt or not prompt.strip():
                        raise RunError(f"{node['title']} needs a prompt.")
                    body = {'model': model, 'prompt': prompt, 'n': 1, 'stream': True, 'steps': int(values['steps']),
                            'sampler': values['sampler'], 'scheduler': values['scheduler'], 'cfg_scale': values['cfg_scale']}
                    seed = value(node, 'seed')
                    body['seed'] = seed if seed is not None else (base_seed + item) % (2**31)
                    negative = value(node, 'negative')
                    if negative and family['negative']:
                        body['negative_prompt'] = negative
                    if family['guidance']:
                        body['guidance'] = values['guidance']
                    init = value(node, 'init')
                    references = [made.get(tuple(one)) for one in sources(node, 'references')]
                    size = value(node, 'size')
                    if size is None and (init or references):
                        source = init or references[0]
                        unit = family['alignment']
                        scale = min(1, math.sqrt(4_000_000 / (source.width * source.height)))
                        size = tuple(max(256, int(side * scale) // unit * unit) for side in source.size)
                    if size:
                        body['size'] = f'{size[0]}x{size[1]}'
                    if init is not None:
                        # The start image is the step's size exactly, so the node prepares it without enlarging it.
                        target = size or init.size
                        if init.size != tuple(target):
                            init = await asyncio.to_thread(init.resize, tuple(target), Image.Resampling.LANCZOS)
                        body['references'] = [encode(init)]
                        body['strength'] = values['strength']
                        body['reference_megapixels'] = max(.1, target[0] * target[1] / 1_000_000)
                    elif references:
                        # References are prepared at no more than the smallest one's size, so none needs an AI enlargement.
                        body['references'] = [encode(one) for one in references]
                        body['reference_megapixels'] = max(.1, min(1, *(one.width * one.height / 1_000_000 for one in references)) // .01 * .01)
                    control = value(node, 'control')
                    if control is not None:
                        body['control'] = {'image': encode(control['image']), 'strength': control['strength'], 'edges': control['edges']}
                    advanced = _advanced(values)
                    if advanced:
                        body['advanced'] = advanced
                    path = '/v1/images/edits' if 'references' in body else '/v1/images/generations'
                else:
                    image = value(node, 'image')
                    previous = origin.get(sources(node, 'image')[0][0]) or (None, None)
                    model = previous[0] or next(iter(graph.models_of(document)), None) or next((one['id'] for one in models), None)
                    if not model:
                        raise RunError(f"{node['title']}: an Upscale step runs on an image model's node; you may use none.")
                    body = {'model': model, 'image': encode(image), 'stream': True,
                            'upscale_with': {'upscaler': values['upscaler'], 'factor': values['factor'], 'tile': int(values['tile'])}}
                    where = holders.get(values['upscaler'])
                    if previous[1] and where is not None and previous[1] in where:
                        body['node'] = previous[1]  # keep the image on the node that made it
                    path = '/v1/images/edits'
                settings[label] = {key: value for key, value in body.items() if key not in ('image', 'references', 'stream', 'n', 'control')}
                if 'control' in body:
                    settings[label]['control'] = {key: one for key, one in body['control'].items() if key != 'image'}
                result = None
                async for event in send(path, body):
                    if event.get('type') == 'error' or 'error' in event:
                        message = (event.get('error') or {}).get('message') if isinstance(event.get('error'), dict) else event.get('error')
                        raise RunError(f"{node['title']}: {message or 'the step failed.'}")
                    if event.get('type') in ('image_generation.completed', 'image_edit.completed'):
                        result = event
                    elif event.get('type', '').endswith('.progress'):
                        yield {**event, 'type': 'workflow.progress', 'step': label, 'item': item}
                if not result:
                    raise RunError(f"{node['title']}: the step ended without an image.")
                if kind == 'sampler':
                    settings[label]['seed'] = result.get('seed', body['seed'])
                made[(label, 'image')] = await asyncio.to_thread(decode, result['b64_json'])
                origin[label] = (model, result.get('node'))
                seeds[label] = result.get('seed') if kind == 'sampler' else seeds.get(sources(node, 'image')[0][0])
                yield {'type': 'workflow.step', 'step': label, 'title': node['title'], 'item': item, 'state': 'completed',
                       'index': finished, 'total': total, 'seed': result.get('seed'), 'node': result.get('node'),
                       'width': result.get('width'), 'height': result.get('height')}
            elif kind in ('output', 'compare'):
                ports = sources(node, 'image' if kind == 'output' else 'images')
                for position, (source, port) in enumerate(ports):
                    image = made.get((source, port))
                    if image is None:
                        continue
                    name = values['label'] if kind == 'output' else f"{values['label']} · {nodes[source]['title']}"
                    yield {'type': 'workflow.output', 'output': name, 'step': label, 'source': source, 'item': item, 'position': position,
                           'b64_json': encode(image), 'width': image.width, 'height': image.height,
                           'seed': seeds.get(source), 'stages': {key: dict(value) for key, value in settings.items()}}
