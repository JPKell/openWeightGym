"""Image workflow graphs: the component catalogue, port types, family capabilities and shipped samples.

One source for the editor (served as JSON), the validator and the sample templates.
Images, masks, text and settings pass between steps; latents never leave a native request
(see docs/advanced-workflow-plan.md).
"""

import json
import math
import re

from mirrorwall import model as _model

# Every sampler and scheduler of the pinned stable-diffusion.cpp revision, by native name;
# the request validator and the editor share them.
NATIVE_SAMPLERS = _model.SAMPLERS
NATIVE_SCHEDULERS = _model.SCHEDULERS
CACHE_MODES = ('off',) + _model.CACHE_MODES
HIRES_UPSCALERS = _model.HIRES_UPSCALERS  # latent methods; installed upscaler files are offered as well
UPSCALER_FILE = _model.UPSCALER_FILE

PORT_TYPES = {
    'text': 'Text', 'model': 'Model', 'image': 'Image', 'mask': 'Mask',
    'seed': 'Seed', 'size': 'Size', 'control': 'Control image',
}

# What each family accepts in this app's runtime: start images (SDXL), ordered references
# (Flux 2), negative prompts, distilled guidance and dimension alignment.
FAMILIES = {
    'flux2-klein': {'label': 'Flux 2 klein', 'negative': False, 'guidance': False, 'init': False, 'references': 4, 'alignment': 16},
    'flux2-dev': {'label': 'Flux 2 Dev', 'negative': False, 'guidance': True, 'init': False, 'references': 4, 'alignment': 16},
    'sdxl': {'label': 'SDXL', 'negative': True, 'guidance': False, 'init': True, 'references': 0, 'alignment': 16},
    'qwen-image': {'label': 'Qwen Image', 'negative': True, 'guidance': False, 'init': False, 'references': 0, 'alignment': 16},
    'qwen-image-2.1': {'label': 'Qwen Image 2.1', 'negative': True, 'guidance': False, 'init': False, 'references': 0, 'alignment': 32},
    'moss-image': {'label': 'MOSS-Image', 'negative': True, 'guidance': False, 'init': False, 'references': 0, 'alignment': 16},
    'krea2': {'label': 'Krea 2', 'negative': True, 'guidance': False, 'init': False, 'references': 0, 'alignment': 16},
}

# The step caches each family's network takes: SDXL is a UNet, the others are DiT block caches (QA-093).
for _name, _family in FAMILIES.items():
    _family['caches'] = list(_model.UNET_CACHES if _name == 'sdxl' else _model.DIT_CACHES)


def uses_advanced(values):
    """Whether a sampler sets any of the native settings a shared-residency model refuses (graph_run._advanced sends them)."""
    return (any(values.get(key) is not None for key in ('eta', 'flow_shift', 'img_cfg', 'shifted_timestep', 'clip_skip'))
            or bool(values.get('vae_tiling')) or values.get('cache_mode', 'off') != 'off' or bool(values.get('hires')))


# Sampler defaults per family; `basis` says where each comes from (docs/advanced-workflow-plan.md).
DEFAULTS = {
    'flux2-klein': {'steps': 6, 'sampler': 'euler', 'scheduler': 'flux2', 'cfg_scale': 1, 'basis': 'App profile; upstream distilled example uses 4 steps'},
    'flux2-dev': {'steps': 28, 'sampler': 'euler', 'scheduler': 'flux2', 'cfg_scale': 1, 'guidance': 4, 'basis': 'App profile and upstream flux2.md'},
    'sdxl': {'steps': 28, 'sampler': 'dpm++2m', 'scheduler': 'karras', 'cfg_scale': 6.5, 'basis': 'Common SDXL practice (community)'},
    'qwen-image': {'steps': 50, 'sampler': 'euler', 'scheduler': 'discrete', 'cfg_scale': 4, 'flow_shift': 3, 'basis': 'App profile; upstream qwen_image.md uses flow shift 3'},
    'qwen-image-2.1': {'steps': 50, 'sampler': 'euler', 'scheduler': 'flux', 'cfg_scale': 6, 'basis': 'Upstream qwen_image_2.1.md'},
    'moss-image': {'steps': 50, 'sampler': 'euler', 'scheduler': 'discrete', 'cfg_scale': 3, 'basis': 'Upstream moss_image.md'},
    'krea2': {'steps': 20, 'sampler': 'euler', 'scheduler': 'discrete', 'cfg_scale': 3.5, 'basis': 'Upstream krea2.md (Raw); Turbo uses 8 steps at CFG 1'},
}


def _field(name, label, kind, default=None, **extra):
    """One editable value of a component; `kind` is text, textarea, number, choice, toggle, model, asset or upscaler.

    An upscaler names an installed upscaler file (ESRGAN family) by file name; nodes resolve it locally.
    A choice with `upscalers` also accepts installed upscaler files after its fixed choices.
    """
    return {'name': name, 'label': label, 'kind': kind, 'default': default, **extra}


def _port(name, label, kind, required=False, multiple=False):
    """One input or output port of a component."""
    return {'name': name, 'label': label, 'type': kind, 'required': required, 'multiple': multiple}


MAX_TILES = 64  # requests one Tiled detail step may make per run item

SAMPLER_FIELDS = [
    _field('steps', 'Steps', 'number', 28, min=1, max=200, step=1, overridable=True),
    _field('sampler', 'Sampler', 'choice', 'euler', choices=list(NATIVE_SAMPLERS), overridable=True),
    _field('scheduler', 'Scheduler', 'choice', 'discrete', choices=list(NATIVE_SCHEDULERS), overridable=True),
    _field('cfg_scale', 'CFG scale', 'number', 1, min=0, max=30, step=0.1, overridable=True),
    _field('guidance', 'Distilled guidance', 'number', 4, min=0, max=20, step=0.1, family='guidance', overridable=True),
    _field('strength', 'Strength (denoise)', 'number', 0.35, min=0, max=1, step=0.01, needs='init', overridable=True),
    _field('flow_shift', 'Flow shift', 'number', None, min=0, max=100, step=0.1, advanced=True, overridable=True),
    _field('eta', 'Eta', 'number', None, min=0, max=2, step=0.01, advanced=True),
    _field('img_cfg', 'Image CFG scale', 'number', None, min=0, max=30, step=0.1, advanced=True),
    _field('shifted_timestep', 'Shifted timestep (SDXL)', 'number', None, min=0, max=1000, step=1, advanced=True),
    _field('clip_skip', 'CLIP skip (SDXL)', 'number', None, min=1, max=12, step=1, advanced=True),
    _field('cache_mode', 'Step cache', 'choice', 'off', choices=list(CACHE_MODES), advanced=True),
    _field('vae_tiling', 'VAE tiling', 'toggle', False, advanced=True),
    _field('hires', 'Hires pass', 'toggle', False, advanced=True),
    _field('hires_upscaler', 'Hires upscaler', 'choice', 'Latent', choices=list(HIRES_UPSCALERS), upscalers=True, needs='hires', advanced=True),
    _field('hires_scale', 'Hires scale', 'number', 1.5, min=1, max=4, step=0.05, needs='hires', advanced=True, overridable=True),
    _field('hires_steps', 'Hires steps', 'number', 14, min=1, max=200, step=1, needs='hires', advanced=True, overridable=True),
    _field('hires_denoise', 'Hires denoise', 'number', 0.38, min=0, max=1, step=0.01, needs='hires', advanced=True, overridable=True),
]

# `phase` is the delivery phase that makes a component runnable; later ones are shown as planned.
COMPONENTS = {
    'prompt': {'label': 'Prompt', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('text', 'Text', 'text')],
               'fields': [_field('text', 'Prompt', 'textarea', '', max=24000)], 'exposed': ['text'],
               'help': 'Text sent to every connected step.'},
    'negative': {'label': 'Negative prompt', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('text', 'Text', 'text')],
                 'fields': [_field('text', 'Negative prompt', 'textarea', '', max=24000)],
                 'help': 'What to avoid. Distilled and turbo models at CFG 1 ignore it.'},
    'combine': {'label': 'Combine text', 'group': 'Inputs', 'phase': 4,
                'inputs': [_port('first', 'First', 'text', True), _port('second', 'Second', 'text', True)],
                'outputs': [_port('text', 'Text', 'text')], 'fields': [_field('separator', 'Separator', 'text', ', ', max=20)],
                'help': 'Joins two texts, for example a subject and a shared style.'},
    'model': {'label': 'Model', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('model', 'Model', 'model')],
              'fields': [_field('model', 'Image model', 'model', '')],
              'help': 'An installed image model with its LoRAs. One model can feed several steps.'},
    'seed': {'label': 'Seed', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('seed', 'Seed', 'seed')],
             'fields': [_field('mode', 'Mode', 'choice', 'random', choices=['random', 'fixed', 'increment']),
                        _field('value', 'Seed', 'number', 1234, min=0, max=2147483647, step=1)], 'exposed': ['value'],
             'help': 'Share one seed between steps to compare them fairly.'},
    'size': {'label': 'Size', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('size', 'Size', 'size')],
             'fields': [_field('width', 'Width', 'number', 1024, min=256, max=4096, step=16),
                        _field('height', 'Height', 'number', 1024, min=256, max=4096, step=16)],
             'help': 'Output size, aligned to the model family.'},
    'load_image': {'label': 'Load image', 'group': 'Inputs', 'phase': 4, 'outputs': [_port('image', 'Image', 'image')],
                   'fields': [_field('asset', 'Image', 'asset', '')], 'exposed': ['asset'],
                   'help': 'An image from your library, or one supplied by the caller.'},
    'sampler': {'label': 'Sampler', 'group': 'Generate', 'phase': 4, 'stage': True,
                'inputs': [_port('model', 'Model', 'model', True), _port('prompt', 'Prompt', 'text', True),
                           _port('negative', 'Negative prompt', 'text'), _port('seed', 'Seed', 'seed'),
                           _port('size', 'Size', 'size'), _port('init', 'Start image', 'image'),
                           _port('references', 'Reference images', 'image', multiple=True),
                           _port('control', 'Control image', 'control')],
                'outputs': [_port('image', 'Image', 'image')], 'fields': SAMPLER_FIELDS,
                'help': 'One generation pass. A start image makes it an image-to-image pass whose strength sets how much changes.'},
    'upscale': {'label': 'Upscale', 'group': 'Refine', 'phase': 4, 'stage': True,
                'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('image', 'Image', 'image')],
                'fields': [_field('upscaler', 'Upscaler', 'upscaler', ''),
                           _field('factor', 'Factor', 'number', 2, min=1, max=4, step=0.5, overridable=True),
                           _field('tile', 'Tile size', 'number', 512, min=128, max=1024, step=64, advanced=True)],
                'help': 'Enlarges with an installed AI upscaler, then resizes exactly to the factor. '
                        'Use several Upscale steps to chain or compare upscalers.'},
    'resize': {'label': 'Resize / crop', 'group': 'Refine', 'phase': 4,
               'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('image', 'Image', 'image')],
               'fields': [_field('mode', 'Mode', 'choice', 'fit', choices=['fit', 'fill', 'crop', 'pad']),
                          _field('width', 'Width', 'number', 1024, min=16, max=8192, step=16),
                          _field('height', 'Height', 'number', 1024, min=16, max=8192, step=16)],
               'help': 'Plain geometry, for example to match a model\'s size before a refine pass.'},
    'control_image': {'label': 'Control image', 'group': 'Structure', 'phase': 5,
                      'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('control', 'Control', 'control')],
                      'fields': [_field('kind', 'Kind', 'choice', 'edges', choices=['edges', 'depth', 'pose']),
                                 _field('strength', 'Control strength', 'number', 0.7, min=0, max=2, step=0.05, overridable=True)],
                      'help': 'Guides composition with the model\'s ControlNet. Edges are found in any photo; for depth or pose, '
                              'give a ready-made depth map or pose skeleton. Match the kind to the ControlNet the model loads.'},
    'tiled_detail': {'label': 'Tiled detail', 'group': 'Refine', 'phase': 5, 'stage': True,
                     'inputs': [_port('model', 'Model', 'model', True), _port('prompt', 'Prompt', 'text', True),
                                _port('negative', 'Negative prompt', 'text'), _port('seed', 'Seed', 'seed'),
                                _port('image', 'Image', 'image', True)],
                     'outputs': [_port('image', 'Image', 'image')],
                     'fields': [_field('strength', 'Strength', 'number', 0.3, min=0, max=1, step=0.01, overridable=True),
                                _field('tile', 'Tile size', 'number', 1024, min=512, max=2048, step=64),
                                _field('overlap', 'Overlap', 'number', 64, min=16, max=256, step=16),
                                *[dict(field) for field in SAMPLER_FIELDS[:4]]],
                     'help': 'Adds detail to a large image tile by tile, each a low-strength image-to-image pass, and blends the seams. '
                             f'At most {MAX_TILES} tiles; a larger tile size makes fewer.'},
    'mask': {'label': 'Mask', 'group': 'Masks', 'phase': 7,
             'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('mask', 'Mask', 'mask')],
             'fields': [_field('invert', 'Invert', 'toggle', False), _field('feather', 'Feather', 'number', 8, min=0, max=128, step=1)],
             'help': 'A painted mask; white areas change.'},
    'composite': {'label': 'Composite', 'group': 'Masks', 'phase': 7,
                  'inputs': [_port('base', 'Base', 'image', True), _port('overlay', 'Overlay', 'image', True),
                             _port('mask', 'Mask', 'mask', True)],
                  'outputs': [_port('image', 'Image', 'image')], 'fields': [],
                  'help': 'Keeps the base outside the mask and the overlay inside it.'},
    'detail': {'label': 'Detail faces', 'group': 'Refine', 'phase': 7, 'stage': True,
               'inputs': [_port('model', 'Model', 'model', True), _port('prompt', 'Prompt', 'text', True),
                          _port('image', 'Image', 'image', True)],
               'outputs': [_port('image', 'Image', 'image')],
               'fields': [_field('strength', 'Strength', 'number', 0.4, min=0, max=1, step=0.01, overridable=True)],
               'help': 'Finds faces with a detector and repaints each one.'},
    'compare': {'label': 'Compare', 'group': 'Outputs', 'phase': 4,
                'inputs': [_port('images', 'Images', 'image', True, multiple=True)],
                'fields': [_field('label', 'Label', 'text', 'Comparison', max=60)],
                'help': 'Shows several results side by side and saves each one.'},
    'output': {'label': 'Output', 'group': 'Outputs', 'phase': 4,
               'inputs': [_port('image', 'Image', 'image', True)],
               'fields': [_field('label', 'Label', 'text', 'Final', max=60)],
               'help': 'Saves the image to the library and returns it to API callers under this label.'},
    'note': {'label': 'Note', 'group': 'Outputs', 'phase': 4, 'fields': [_field('text', 'Note', 'textarea', '', max=4000)],
             'help': 'Text on the canvas; not run.'},
}

LIMITS = {'nodes': 64, 'links': 128, 'stages': 32, 'images': 128}


def _node(identifier, component, at, title=None, **values):
    """A sample graph node of `component` at canvas position `at`, with the component's defaults under `values`."""
    defaults = {field['name']: field['default'] for field in COMPONENTS[component].get('fields', [])}
    return {'id': identifier, 'type': component, 'title': title or COMPONENTS[component]['label'],
            'values': {**defaults, **values}, 'at': at}


def _link(source, target):
    """A link between two `node.port` names."""
    return {'from': source.split('.'), 'to': target.split('.')}


def _graph(name, summary, family, nodes, links, exposed=None, overridable=None):
    """A complete sample image workflow document, with each node's exposed and overridable fields."""
    for node in nodes:
        node['exposed'] = (exposed or {}).get(node['id'], COMPONENTS[node['type']].get('exposed', []))
        node['overridable'] = (overridable or {}).get(node['id'], {})
    return {'schema': 1, 'name': name, 'summary': summary, 'family': family, 'media': 'image',
            'nodes': nodes, 'links': [_link(*pair) for pair in links]}


def _inputs(prompt='A lighthouse on a basalt cliff at dusk, long exposure sea, warm windows, cinematic',
            model='', at_y=40, negative=False):
    """The prompt, model, seed and size nodes (and optionally a negative prompt) that start a sample graph."""
    nodes = [_node('prompt', 'prompt', [40, at_y], text=prompt),
             _node('model', 'model', [40, at_y + 230], model=model),
             _node('seed', 'seed', [40, at_y + 360], mode='fixed', value=1234),
             _node('size', 'size', [40, at_y + 490], width=1024, height=1024)]
    if negative:
        nodes.insert(1, _node('negative', 'negative', [40, at_y + 640], text='blurry, low detail, watermark'))
    return nodes


def _sampler(identifier, family, at, title=None, **values):
    """A sampler node with the model family's default settings."""
    defaults = {k: v for k, v in DEFAULTS[family].items() if k != 'basis'}
    return _node(identifier, 'sampler', at, title, **{**defaults, **values})


SAMPLES = [
    _graph('Quick draft', 'Prompt to image with the model family defaults.', 'flux2-klein',
           [*_inputs(model='klein-4b'), _sampler('base', 'flux2-klein', [380, 160], 'Base'),
            _node('out', 'output', [720, 160], label='Final'),
            _node('note', 'note', [720, 360], text='Start here: change the model, then the steps. Everything else follows the family defaults.')],
           [('prompt.text', 'base.prompt'), ('model.model', 'base.model'), ('seed.seed', 'base.seed'),
            ('size.size', 'base.size'), ('base.image', 'out.image')],
           overridable={'base': {'steps': [4, 12]}}),
    _graph('SDXL two-pass hires', 'One sampler with a latent hires pass, then an AI upscale.', 'sdxl',
           [*_inputs(model='sdxl-base', negative=True),
            _sampler('base', 'sdxl', [380, 160], 'Base with hires', hires=True, hires_upscaler='Latent', hires_scale=1.5,
                     hires_steps=14, hires_denoise=0.38),
            _node('upscale', 'upscale', [720, 160], upscaler='4x-UltraSharp.pth', factor=2), _node('out', 'output', [1040, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('negative.text', 'base.negative'), ('model.model', 'base.model'),
            ('seed.seed', 'base.seed'), ('size.size', 'base.size'), ('base.image', 'upscale.image'), ('upscale.image', 'out.image')],
           exposed={'negative': ['text']}, overridable={'base': {'hires_denoise': [0.2, 0.5], 'steps': [20, 40]}}),
    _graph('SDXL two-model refine', 'A base SDXL model composes; a second SDXL checkpoint refines at low strength.', 'sdxl',
           [*_inputs(model='sdxl-base', negative=True),
            _node('model2', 'model', [380, 420], 'Refine model', model='sdxl-photo'),
            _sampler('base', 'sdxl', [380, 120], 'Base'),
            _sampler('refine', 'sdxl', [720, 220], 'Refine', steps=14, strength=0.25),
            _node('out', 'output', [1040, 220], label='Final')],
           [('prompt.text', 'base.prompt'), ('prompt.text', 'refine.prompt'), ('negative.text', 'base.negative'),
            ('negative.text', 'refine.negative'), ('model.model', 'base.model'), ('model2.model', 'refine.model'),
            ('seed.seed', 'base.seed'), ('seed.seed', 'refine.seed'), ('size.size', 'base.size'),
            ('base.image', 'refine.init'), ('refine.image', 'out.image')],
           overridable={'refine': {'strength': [0.1, 0.45], 'steps': [8, 24]}}),
    _graph('Sampler shoot-out', 'One prompt, model and seed through four samplers, side by side.', 'sdxl',
           [*_inputs(model='sdxl-base'),
            *[_sampler(key, 'sdxl', [380, 40 + 180 * index], label, sampler=sampler, scheduler=scheduler)
              for index, (key, label, sampler, scheduler) in enumerate([
                  ('euler', 'Euler · simple', 'euler', 'simple'), ('dpm', 'DPM++ 2M · Karras', 'dpm++2m', 'karras'),
                  ('res', 'Res multistep · beta', 'res_multistep', 'beta'), ('ersde', 'ER-SDE · KL optimal', 'er_sde', 'kl_optimal')])],
            _node('compare', 'compare', [720, 260], label='Samplers')],
           [*[(source, f'{key}.{port}') for key in ('euler', 'dpm', 'res', 'ersde')
              for source, port in (('prompt.text', 'prompt'), ('model.model', 'model'), ('seed.seed', 'seed'), ('size.size', 'size'))],
            *[(f'{key}.image', 'compare.images') for key in ('euler', 'dpm', 'res', 'ersde')]]),
    _graph('Upscaler comparison', 'One SDXL image enlarged by three upscalers, side by side, plus a chained 4× pass.', 'sdxl',
           [*_inputs(model='sdxl-base'), _sampler('base', 'sdxl', [380, 160], 'Base'),
            *[_node(key, 'upscale', [720, 40 + 180 * index], label, upscaler=file, factor=2)
              for index, (key, label, file) in enumerate([('sharp', 'UltraSharp 2×', '4x-UltraSharp.pth'),
                                                          ('real', 'RealESRGAN 2×', 'RealESRGAN_x4plus.pth'),
                                                          ('siax', 'Siax 2×', '4x_NMKD-Siax_200k.pth')])],
            _node('again', 'upscale', [1040, 40], 'UltraSharp then 2× more', upscaler='4x-UltraSharp.pth', factor=2),
            _node('compare', 'compare', [1360, 220], label='Upscalers')],
           [('prompt.text', 'base.prompt'), ('model.model', 'base.model'), ('seed.seed', 'base.seed'), ('size.size', 'base.size'),
            *[('base.image', f'{key}.image') for key in ('sharp', 'real', 'siax')], ('sharp.image', 'again.image'),
            *[(f'{key}.image', 'compare.images') for key in ('sharp', 'real', 'siax', 'again')]]),
    _graph('Family comparison', 'One prompt and seed through three model families.', 'flux2-klein',
           [_node('prompt', 'prompt', [40, 40], text='A ceramic teapot shaped like a sleeping fox, studio product photo'),
            _node('seed', 'seed', [40, 280], mode='fixed', value=77),
            *[_node(f'm{index}', 'model', [380, 40 + 230 * index], label, model=model)
              for index, (label, model) in enumerate([('Klein 4B', 'klein-4b'), ('Krea 2 Turbo', 'krea2-turbo'), ('Qwen Image', 'qwen-image')])],
            _sampler('klein', 'flux2-klein', [720, 40], 'Klein 4B'),
            _sampler('krea', 'krea2', [720, 270], 'Krea 2 Turbo', steps=8, cfg_scale=1),
            _sampler('qwen', 'qwen-image', [720, 500], 'Qwen Image'),
            _node('compare', 'compare', [1060, 270], label='Families')],
           [*[('prompt.text', f'{key}.prompt') for key in ('klein', 'krea', 'qwen')],
            *[('seed.seed', f'{key}.seed') for key in ('klein', 'krea', 'qwen')],
            ('m0.model', 'klein.model'), ('m1.model', 'krea.model'), ('m2.model', 'qwen.model'),
            *[(f'{key}.image', 'compare.images') for key in ('klein', 'krea', 'qwen')]]),
    _graph('Krea Turbo draft → SDXL detail', 'A fast composition, enlarged, then detailed by SDXL at low strength.', 'krea2',
           [*_inputs(model='krea2-turbo'), _node('model2', 'model', [380, 420], 'Detail model', model='sdxl-base'),
            _sampler('draft', 'krea2', [380, 120], 'Draft', steps=8, cfg_scale=1),
            _node('upscale', 'upscale', [720, 120], upscaler='4x-UltraSharp.pth', factor=2),
            _sampler('detail', 'sdxl', [1040, 220], 'Detail', steps=20, strength=0.3),
            _node('out', 'output', [1360, 220], label='Final'), _node('draft_out', 'output', [1040, 20], label='Draft')],
           [('prompt.text', 'draft.prompt'), ('prompt.text', 'detail.prompt'), ('model.model', 'draft.model'),
            ('model2.model', 'detail.model'), ('seed.seed', 'draft.seed'), ('seed.seed', 'detail.seed'), ('size.size', 'draft.size'),
            ('draft.image', 'upscale.image'), ('draft.image', 'draft_out.image'), ('upscale.image', 'detail.init'),
            ('detail.image', 'out.image')]),
    _graph('Klein multi-reference edit', 'Up to four ordered reference images guide one instruction.', 'flux2-klein',
           [_node('prompt', 'prompt', [40, 40], text='Put the jacket from image 1 on the person in image 2, in the room from image 3'),
            *[_node(f'ref{index}', 'load_image', [40, 240 + 130 * index], f'Reference {index + 1}') for index in range(3)],
            _node('model', 'model', [380, 420], model='klein-4b'),
            _sampler('edit', 'flux2-klein', [380, 120], 'Edit', steps=4),
            _node('out', 'output', [720, 120], label='Edited')],
           [('prompt.text', 'edit.prompt'), ('model.model', 'edit.model'),
            *[(f'ref{index}.image', 'edit.references') for index in range(3)], ('edit.image', 'out.image')],
           exposed={'prompt': ['text'], 'ref0': ['asset'], 'ref1': ['asset'], 'ref2': ['asset']}),
    _graph('Qwen Lightning fast', 'An 8-step Lightning LoRA at CFG 1 (community settings; needs the LoRA).', 'qwen-image',
           [*_inputs(model='qwen-image'),
            _sampler('base', 'qwen-image', [380, 160], 'Lightning', steps=8, scheduler='simple', cfg_scale=1, flow_shift=3),
            _node('out', 'output', [720, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('model.model', 'base.model'), ('seed.seed', 'base.seed'),
            ('size.size', 'base.size'), ('base.image', 'out.image')]),
    _graph('Structure-guided generation', 'Edges found in a photo guide a new image; needs an SDXL model entry with a ControlNet.', 'sdxl',
           [*_inputs(model='sdxl-base'), _node('source', 'load_image', [380, 420], 'Structure source'),
            _node('control', 'control_image', [380, 560], kind='edges', strength=0.7),
            _sampler('base', 'sdxl', [720, 160], 'Base'), _node('out', 'output', [1040, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('model.model', 'base.model'), ('seed.seed', 'base.seed'), ('size.size', 'base.size'),
            ('source.image', 'control.image'), ('control.control', 'base.control'), ('base.image', 'out.image')],
           exposed={'prompt': ['text'], 'source': ['asset']}),
    _graph('Tiled 4× upscale', 'Enlarge twice with a tiled detail pass in between.', 'sdxl',
           [*_inputs(model='sdxl-base'), _sampler('base', 'sdxl', [380, 160], 'Base'),
            _node('up1', 'upscale', [720, 60], 'Upscale 2×', upscaler='4x-UltraSharp.pth', factor=2),
            _node('tiles', 'tiled_detail', [1040, 160], strength=0.3, tile=1024, overlap=64, steps=20, sampler='dpm++2m', scheduler='karras', cfg_scale=6.5),
            _node('up2', 'upscale', [1360, 60], 'Upscale 2× again', upscaler='4x-UltraSharp.pth', factor=2), _node('out', 'output', [1680, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('prompt.text', 'tiles.prompt'), ('model.model', 'base.model'),
            ('model.model', 'tiles.model'), ('seed.seed', 'base.seed'), ('seed.seed', 'tiles.seed'), ('size.size', 'base.size'),
            ('base.image', 'up1.image'), ('up1.image', 'tiles.image'), ('tiles.image', 'up2.image'), ('up2.image', 'out.image')]),
]


def catalogue():
    """Everything the editor needs, as one JSON-ready object."""
    return {'components': COMPONENTS, 'order': list(COMPONENTS), 'port_types': PORT_TYPES, 'families': FAMILIES, 'defaults': DEFAULTS,
            'samplers': list(NATIVE_SAMPLERS), 'schedulers': list(NATIVE_SCHEDULERS), 'hires_upscalers': list(HIRES_UPSCALERS),
            'sampler_aliases': dict(_model.SAMPLER_ALIASES), 'limits': LIMITS, 'runnable_phase': RUNNABLE_PHASE}


# ---- Validation: the server's copy of the editor's rules (weightroom/static/workflow-graph.js) ----

LABEL = re.compile(r'[a-z][a-z0-9_]{0,39}')
SLUG = re.compile(r'[a-z0-9][a-z0-9._-]{0,79}')
MAX_DOCUMENT_BYTES = 256 * 1024
RUNNABLE_PHASE = 5  # components of later phases are saved but cannot run yet


def slug(name):
    """The API name a workflow name gives, as the editor derives it."""
    value = re.sub(r'[^a-z0-9._-]+', '-', str(name or 'workflow').lower()).strip('-')[:80]
    return value or 'workflow'


def _value(field, value):
    """Check one component value against its field; None means unset for optional advanced numbers."""
    kind, label = field['kind'], field['label']
    if kind == 'number':
        if value is None and field['default'] is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or not field.get('min', -math.inf) <= value <= field.get('max', math.inf):
            raise ValueError(f"{label} must be a number from {field.get('min')} to {field.get('max')}.")
        return value
    if kind == 'toggle':
        if type(value) is not bool:
            raise ValueError(f'{label} must be on or off.')
        return value
    if kind == 'choice':
        if value not in field['choices'] and not (field.get('upscalers') and isinstance(value, str) and UPSCALER_FILE.fullmatch(value)):
            raise ValueError(f'{label} must be one of: ' + ', '.join(map(str, field['choices'])) +
                             (' or an installed upscaler file' if field.get('upscalers') else '') + '.')
        return value
    if kind == 'upscaler':
        if not isinstance(value, str) or (value and not UPSCALER_FILE.fullmatch(value)):
            raise ValueError(f'{label} must name an upscaler file, such as 4x-UltraSharp.pth.')
        return value
    if not isinstance(value, str) or len(value) > field.get('max', 200) or '\0' in value:
        raise ValueError(f"{label} must be text of at most {field.get('max', 200)} characters.")
    return value


def _ports(node, side):
    """A node's input or output ports (`side`), by name."""
    return {port['name']: port for port in COMPONENTS[node['type']].get(side, [])}


def check(document, models=None, upscalers=None):
    """Validate a workflow document. Structural faults raise ValueError; run problems are returned.

    `models` lists the installed image models as {'id', 'family'} and `upscalers` the installed
    upscaler file names; without them, the dependent rules are skipped.
    Returns (normalized document, [{'node', 'level', 'text'}]).
    """
    if not isinstance(document, dict):
        raise ValueError('A workflow must be a JSON object.')
    if len(json.dumps(document)) > MAX_DOCUMENT_BYTES:
        raise ValueError('A workflow document may be at most 256 KiB.')
    unknown = document.keys() - {'schema', 'name', 'summary', 'family', 'media', 'api_name', 'nodes', 'links'}
    if unknown:
        raise ValueError('Unsupported workflow fields: ' + ', '.join(sorted(unknown)) + '.')
    if document.get('schema', 1) != 1 or document.get('media', 'image') != 'image':
        raise ValueError('This version supports schema 1 image workflows.')
    name = document.get('name')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('A workflow name must contain 1–100 characters.')
    api_name = document.get('api_name') or slug(name)
    if not isinstance(api_name, str) or not SLUG.fullmatch(api_name):
        raise ValueError('The API name may use lowercase letters, digits, dots, dashes and underscores (up to 80).')
    summary = document.get('summary', '')
    if not isinstance(summary, str) or len(summary) > 400:
        raise ValueError('A summary may be at most 400 characters.')
    nodes, links = document.get('nodes'), document.get('links', [])
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError('A workflow needs nodes and links lists.')
    if len(nodes) > LIMITS['nodes'] or len(links) > LIMITS['links']:
        raise ValueError(f"Use at most {LIMITS['nodes']} components and {LIMITS['links']} links.")
    clean, seen = [], set()
    for node in nodes:
        if not isinstance(node, dict) or node.keys() - {'id', 'type', 'title', 'values', 'at', 'lane', 'exposed', 'overridable'}:
            raise ValueError('Each component has id, type, title, values, at, lane, exposed and overridable only.')
        identifier, kind = node.get('id'), node.get('type')
        if not isinstance(identifier, str) or not LABEL.fullmatch(identifier):
            raise ValueError('Step labels start with a letter and use lowercase letters, digits and underscores (up to 40).')
        if identifier in seen:
            raise ValueError('Two components share the label ' + identifier + '.')
        seen.add(identifier)
        if kind not in COMPONENTS:
            raise ValueError(f'Unknown component type for {identifier}.')
        component, fields = COMPONENTS[kind], {field['name']: field for field in COMPONENTS[kind].get('fields', [])}
        title = node.get('title', component['label'])
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60:
            raise ValueError(f'{identifier}: a name must contain 1–60 characters.')
        values = node.get('values', {})
        if not isinstance(values, dict) or values.keys() - fields.keys():
            raise ValueError(f'{identifier}: unknown settings ' + ', '.join(sorted(set(values) - set(fields))) + '.')
        values = {name: _value(field, values.get(name, field['default'])) for name, field in fields.items()}
        at = node.get('at', [0, 0])
        if (not isinstance(at, list) or len(at) != 2 or any(type(one) not in (int, float) or not math.isfinite(one)
                                                            or not -10000 <= one <= 100000 for one in at)):
            raise ValueError(f'{identifier}: a position is two numbers.')
        lane = node.get('lane')
        if lane is not None and (not isinstance(lane, list) or len(lane) != 2 or any(
                one is not None and (type(one) not in (int, float) or not math.isfinite(one) or not 0 <= one <= 500) for one in lane)):
            raise ValueError(f'{identifier}: a lane position is [lane or null, order or null].')
        exposed = node.get('exposed', [])
        if not isinstance(exposed, list) or len(set(exposed)) != len(exposed) or any(one not in fields for one in exposed):
            raise ValueError(f'{identifier}: callers may only set this component\'s own fields.')
        overridable = node.get('overridable', {})
        if not isinstance(overridable, dict):
            raise ValueError(f'{identifier}: overridable fields map a field to [lowest, highest].')
        bounds = {}
        for name, span in overridable.items():
            field = fields.get(name)
            if not field or not field.get('overridable') or field['kind'] != 'number':
                raise ValueError(f'{identifier}: {name} cannot be overridden by callers.')
            if (not isinstance(span, list) or len(span) != 2 or any(type(one) not in (int, float) or not math.isfinite(one) for one in span)
                    or not field.get('min', -math.inf) <= span[0] <= span[1] <= field.get('max', math.inf)):
                raise ValueError(f"{identifier}: {field['label']} bounds must lie within {field.get('min')}–{field.get('max')}, lowest first.")
            bounds[name] = list(span)
        if set(exposed) & set(bounds):
            raise ValueError(f'{identifier}: a field is either set by callers or overridable within bounds, not both.')
        clean.append({'id': identifier, 'type': kind, 'title': title.strip(), 'values': values, 'at': list(at),
                      **({'lane': list(lane)} if lane is not None else {}), 'exposed': list(exposed), 'overridable': bounds})
    found = {node['id']: node for node in clean}
    clean_links, into = [], {}
    for link in links:
        if (not isinstance(link, dict) or link.keys() - {'auto'} != {'from', 'to'} or not isinstance(link.get('auto', False), bool) or
                any(not isinstance(end, list) or len(end) != 2 or not all(isinstance(one, str) for one in end) for end in (link['from'], link['to']))):
            raise ValueError('Each link is {"from": [label, output], "to": [label, input]}.')
        (source, output), (target, entry) = link['from'], link['to']
        if source not in found or target not in found:
            raise ValueError('A link names a component that does not exist.')
        out_port, in_port = _ports(found[source], 'outputs').get(output), _ports(found[target], 'inputs').get(entry)
        if not out_port or not in_port:
            raise ValueError(f'A link from {source} to {target} names a port that does not exist.')
        if out_port['type'] != in_port['type']:
            raise ValueError(f"{found[target]['title']} · {in_port['label']} takes {PORT_TYPES[in_port['type']]}, not {PORT_TYPES[out_port['type']]}.")
        key = (target, entry)
        if [source, output] in into.get(key, []):
            raise ValueError('A link is listed twice.')
        if into.get(key) and not in_port['multiple']:
            raise ValueError(f"{found[target]['title']} · {in_port['label']} takes one source.")
        into.setdefault(key, []).append([source, output])
        clean_links.append({'from': [source, output], 'to': [target, entry]})
    remaining, pending = set(found), list(clean_links)
    while remaining:
        free = {one for one in remaining if not any(link['to'][0] == one for link in pending)}
        if not free:
            raise ValueError('The links form a loop; an image cannot depend on itself.')
        remaining -= free
        pending = [link for link in pending if link['from'][0] not in free]
    result = {'schema': 1, 'name': name.strip(), 'api_name': api_name, 'summary': summary, 'media': 'image',
              'nodes': clean, 'links': clean_links}
    if isinstance(document.get('family'), str) and document['family'] in FAMILIES:
        result['family'] = document['family']
    return result, problems(result, models, upscalers)


def problems(document, models=None, upscalers=None):
    """Run problems of a structurally valid document: missing sources, family rules, planned components, upscalers."""
    found, listed = {node['id']: node for node in document['nodes']}, []
    def push(node, level, text):
        """Record one validation problem, optionally about a node."""
        listed.append({'node': node and node['id'], 'level': level, 'text': text})
    def into(node, port):
        """The IDs of the nodes linked into a node's port."""
        return [link['from'][0] for link in document['links'] if link['to'] == [node['id'], port]]
    if not any(node['type'] in ('output', 'compare') for node in document['nodes']):
        push(None, 'error', 'Add an Output so the run saves something.')
    installed = {model['id']: model for model in models or []}
    stages = 0
    for node in document['nodes']:
        component = COMPONENTS[node['type']]
        stages += bool(component.get('stage'))
        if component['phase'] > RUNNABLE_PHASE:
            push(node, 'error', f"{component['label']} is planned for a later phase; it cannot run yet.")
        for port in component.get('inputs', []):
            if port['required'] and not into(node, port['name']):
                push(node, 'error', f"{port['label']} needs a source.")
        if node['type'] == 'upscale' and not node['values']['upscaler']:
            push(node, 'error', 'Choose an upscaler.')
        wanted = [node['values'].get('upscaler')] if node['type'] == 'upscale' else (
            [node['values']['hires_upscaler']] if node['type'] == 'sampler' and node['values']['hires'] else [])
        if upscalers is not None and any(one and one not in HIRES_UPSCALERS and one not in upscalers for one in wanted):
            push(node, 'error', 'That upscaler is not installed on any node.')
        if node['type'] == 'sampler' and node['values']['hires'] and node['values']['hires_upscaler'] not in HIRES_UPSCALERS:
            push(node, 'error', 'A hires pass with an upscaler file cannot run yet; choose a latent method or add an Upscale step.')
        if models is None:
            continue
        if node['type'] == 'model' and node['values']['model'] not in installed:
            push(node, 'error', 'Choose an installed image model.')
        if node['type'] == 'tiled_detail':
            source = next(iter(into(node, 'model')), None)
            model = installed.get(found[source]['values']['model']) if source else None
            family = FAMILIES.get(model.get('family')) if model else None
            if family and not family['init']:
                push(node, 'error', f"{family['label']} has no image-to-image pass here; tiled detail needs an SDXL model.")
            elif family and node['values']['tile'] % family['alignment']:
                push(node, 'error', f"{family['label']} needs a tile size in multiples of {family['alignment']}.")
            if node['values']['overlap'] * 2 >= node['values']['tile']:
                push(node, 'error', 'The overlap must be less than half the tile size.')
            continue
        if node['type'] == 'sampler':
            source = next(iter(into(node, 'model')), None)
            model = installed.get(found[source]['values']['model']) if source else None
            family = FAMILIES.get(model.get('family')) if model else None
            if not family:
                continue
            if into(node, 'control') and not model.get('control'):
                push(node, 'error', 'This model has no ControlNet. Add one to an exclusive SDXL model entry under Models → Images, '
                                    'or remove the control image.')
            references = len(into(node, 'references'))
            if references > family['references']:
                push(node, 'error', f"{family['label']} uses at most {family['references']} reference images." if family['references']
                     else f"{family['label']} does not use reference images here.")
            if into(node, 'init') and not family['init']:
                push(node, 'error', f"{family['label']} has no start-image pass here; use reference images or an SDXL model.")
            if into(node, 'negative') and (not family['negative'] or node['values']['cfg_scale'] <= 1):
                push(node, 'warning', 'The negative prompt has no effect at CFG 1 or with this family.')
            sized = next(iter(into(node, 'size')), None)
            if sized and (found[sized]['values']['width'] % family['alignment'] or found[sized]['values']['height'] % family['alignment']):
                push(node, 'error', f"{family['label']} needs width and height in multiples of {family['alignment']}.")
            if model.get('family') == 'sdxl' and node['values'].get('img_cfg') not in (None, 1) and not into(node, 'init'):
                push(node, 'error', 'On SDXL, Image CFG scale applies to a pass with a start image; leave it empty or 1 here.')
            cache = node['values'].get('cache_mode', 'off')
            if cache != 'off' and cache not in family['caches']:
                push(node, 'error', f"The {cache} cache does not apply to {family['label']}; choose "
                                    + ' or '.join(['off', *family['caches']]) + '.')
            if not model.get('exclusive', True) and uses_advanced(node['values']):
                push(node, 'error', 'Advanced settings need an exclusive model entry; this one shares residency with a measured '
                                    'profile. Clear them, or choose an exclusive entry under Models → Images.')
            if model.get('family') == 'sdxl' and node['values'].get('flow_shift') is not None:
                push(node, 'error', 'Flow shift applies to flow-matching models, not SDXL.')
            if model.get('family') != 'sdxl' and any(node['values'].get(key) is not None for key in ('clip_skip', 'shifted_timestep')):
                push(node, 'error', 'CLIP skip and shifted timestep apply to SDXL models.')
    if stages > LIMITS['stages']:
        push(None, 'error', f"Use at most {LIMITS['stages']} steps.")
    return listed


def models_of(document):
    """The image models a workflow uses; a caller may run it only with access to all of them."""
    return sorted({node['values']['model'] for node in document['nodes'] if node['type'] == 'model' and node['values']['model']})


def interface(document):
    """What a caller of a published workflow sees: inputs it may set, overridable bounds, outputs and models."""
    inputs, overrides = [], []
    for node in document['nodes']:
        fields = {field['name']: field for field in COMPONENTS[node['type']].get('fields', [])}
        for name in node['exposed']:
            field = fields[name]
            inputs.append({'name': node['id'] if node['type'] == 'load_image' else node['id'] + '.' + name,
                           'label': node['title'] + ' · ' + field['label'], 'kind': field['kind'],
                           'default': None if node['type'] == 'load_image' else node['values'][name],
                           **{key: field[key] for key in ('min', 'max', 'choices') if key in field}})
        for name, span in node['overridable'].items():
            overrides.append({'name': node['id'] + '.' + name, 'label': node['title'] + ' · ' + fields[name]['label'],
                              'default': node['values'][name], 'min': span[0], 'max': span[1]})
    outputs = [{'step': node['id'], 'label': node['values']['label']} for node in document['nodes'] if node['type'] in ('output', 'compare')]
    return {'name': document['name'], 'api_name': document['api_name'], 'summary': document.get('summary', ''),
            'models': models_of(document), 'inputs': inputs, 'overrides': overrides, 'outputs': outputs}
