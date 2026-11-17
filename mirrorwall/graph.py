"""Image workflow graphs: the component catalogue, port types, family capabilities and shipped samples.

One source for the editor (served as JSON), the validator and the sample templates.
Images, masks, text and settings pass between steps; latents never leave a native request
(see docs/advanced-workflow-plan.md).
"""

from mirrorwall import model as _model

# Every sampler and scheduler of the pinned stable-diffusion.cpp revision, by native name;
# the request validator and the editor share them.
NATIVE_SAMPLERS = _model.SAMPLERS
NATIVE_SCHEDULERS = _model.SCHEDULERS
CACHE_MODES = ('off',) + _model.CACHE_MODES
HIRES_UPSCALERS = _model.HIRES_UPSCALERS  # model upscaling is the separate Upscale step

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
    """One editable value of a component; `kind` is text, textarea, number, choice, toggle, model or asset."""
    return {'name': name, 'label': label, 'kind': kind, 'default': default, **extra}


def _port(name, label, kind, required=False, multiple=False):
    return {'name': name, 'label': label, 'type': kind, 'required': required, 'multiple': multiple}


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
    _field('hires_upscaler', 'Hires upscaler', 'choice', 'Latent', choices=list(HIRES_UPSCALERS), needs='hires', advanced=True),
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
                'fields': [_field('factor', 'Factor', 'number', 2, min=1, max=4, step=0.5, overridable=True),
                           _field('upscaler', 'Upscaler', 'choice', 'Model upscaler', choices=['Model upscaler'])],
                'help': 'Enlarges with the configured AI upscaler; never a silent plain resize.'},
    'resize': {'label': 'Resize / crop', 'group': 'Refine', 'phase': 4,
               'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('image', 'Image', 'image')],
               'fields': [_field('mode', 'Mode', 'choice', 'fit', choices=['fit', 'fill', 'crop', 'pad']),
                          _field('width', 'Width', 'number', 1024, min=16, max=8192, step=16),
                          _field('height', 'Height', 'number', 1024, min=16, max=8192, step=16)],
               'help': 'Plain geometry, for example to match a model\'s size before a refine pass.'},
    'control_image': {'label': 'Control image', 'group': 'Structure', 'phase': 5,
                      'inputs': [_port('image', 'Image', 'image', True)], 'outputs': [_port('control', 'Control', 'control')],
                      'fields': [_field('kind', 'Kind', 'choice', 'depth', choices=['depth', 'edges', 'pose']),
                                 _field('strength', 'Control strength', 'number', 0.7, min=0, max=2, step=0.05, overridable=True)],
                      'help': 'Guides composition from an image\'s depth, edges or pose.'},
    'tiled_detail': {'label': 'Tiled detail', 'group': 'Refine', 'phase': 5, 'stage': True,
                     'inputs': [_port('model', 'Model', 'model', True), _port('prompt', 'Prompt', 'text', True),
                                _port('image', 'Image', 'image', True)],
                     'outputs': [_port('image', 'Image', 'image')],
                     'fields': [_field('strength', 'Strength', 'number', 0.3, min=0, max=1, step=0.01, overridable=True),
                                _field('tile', 'Tile size', 'number', 1024, min=512, max=2048, step=64),
                                _field('overlap', 'Overlap', 'number', 64, min=16, max=256, step=16)],
                     'help': 'Adds detail to a large image tile by tile and blends the seams.'},
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
    defaults = {field['name']: field['default'] for field in COMPONENTS[component].get('fields', [])}
    return {'id': identifier, 'type': component, 'title': title or COMPONENTS[component]['label'],
            'values': {**defaults, **values}, 'at': at}


def _link(source, target):
    return {'from': source.split('.'), 'to': target.split('.')}


def _graph(name, summary, family, nodes, links, exposed=None, overridable=None):
    for node in nodes:
        node['exposed'] = (exposed or {}).get(node['id'], COMPONENTS[node['type']].get('exposed', []))
        node['overridable'] = (overridable or {}).get(node['id'], {})
    return {'schema': 1, 'name': name, 'summary': summary, 'family': family, 'media': 'image',
            'nodes': nodes, 'links': [_link(*pair) for pair in links]}


def _inputs(prompt='A lighthouse on a basalt cliff at dusk, long exposure sea, warm windows, cinematic',
            model='', at_y=40, negative=False):
    nodes = [_node('prompt', 'prompt', [40, at_y], text=prompt),
             _node('model', 'model', [40, at_y + 230], model=model),
             _node('seed', 'seed', [40, at_y + 360], mode='fixed', value=1234),
             _node('size', 'size', [40, at_y + 490], width=1024, height=1024)]
    if negative:
        nodes.insert(1, _node('negative', 'negative', [40, at_y + 640], text='blurry, low detail, watermark'))
    return nodes


def _sampler(identifier, family, at, title=None, **values):
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
            _node('upscale', 'upscale', [720, 160], factor=2), _node('out', 'output', [1040, 160], label='Final')],
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
            _node('upscale', 'upscale', [720, 120], factor=2),
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
    _graph('Structure-guided generation', 'Depth from a photo guides a new image (Phase 5).', 'sdxl',
           [*_inputs(model='sdxl-base'), _node('source', 'load_image', [380, 420], 'Structure source'),
            _node('control', 'control_image', [380, 560], kind='depth', strength=0.7),
            _sampler('base', 'sdxl', [720, 160], 'Base'), _node('out', 'output', [1040, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('model.model', 'base.model'), ('seed.seed', 'base.seed'), ('size.size', 'base.size'),
            ('source.image', 'control.image'), ('control.control', 'base.control'), ('base.image', 'out.image')]),
    _graph('Tiled 4× upscale', 'Enlarge twice with a tiled detail pass in between (Phase 5).', 'sdxl',
           [*_inputs(model='sdxl-base'), _sampler('base', 'sdxl', [380, 160], 'Base'),
            _node('up1', 'upscale', [720, 60], 'Upscale 2×', factor=2),
            _node('tiles', 'tiled_detail', [1040, 160], strength=0.3, tile=1024, overlap=64),
            _node('up2', 'upscale', [1360, 60], 'Upscale 2× again', factor=2), _node('out', 'output', [1680, 160], label='Final')],
           [('prompt.text', 'base.prompt'), ('prompt.text', 'tiles.prompt'), ('model.model', 'base.model'),
            ('model.model', 'tiles.model'), ('seed.seed', 'base.seed'), ('size.size', 'base.size'), ('base.image', 'up1.image'),
            ('up1.image', 'tiles.image'), ('tiles.image', 'up2.image'), ('up2.image', 'out.image')]),
]


def catalogue():
    """Everything the editor needs, as one JSON-ready object."""
    return {'components': COMPONENTS, 'order': list(COMPONENTS), 'port_types': PORT_TYPES, 'families': FAMILIES, 'defaults': DEFAULTS,
            'samplers': list(NATIVE_SAMPLERS), 'schedulers': list(NATIVE_SCHEDULERS), 'limits': LIMITS}

