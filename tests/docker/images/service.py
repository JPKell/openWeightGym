"""Real OWG services with disposable identities and tiny, non-inference image assets."""
import json
from pathlib import Path
import secrets
import struct
import sys

import uvicorn

from loadcoach.config import model_places


def base(name):
    return Path('/state') / name


def places(name):
    return dict(model_places(str(base(name) / 'cache'), {}, str(base(name) / 'storage')))


def private_file(path, value):
    path.write_text(value)
    path.chmod(0o600)


def tensor_file(path, tensors, payload=b''):
    """Metadata fixtures exercise discovery and copying without multi-GB weights."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps({name: {'dtype': 'F16', 'shape': list(shape), 'data_offsets': [0, 0]}
                         for name, shape in tensors.items()}).encode()
    path.write_bytes(struct.pack('<Q', len(header)) + header + payload)


def seed():
    from loadcoach.groups import Group
    from loadcoach.users import Users
    if (base('front') / 'test-session.key').exists():
        return
    for name in ('front', 'compute', 'rig'):
        base(name).mkdir(parents=True, exist_ok=True)
        for folder in places(name).values():
            Path(folder).mkdir(parents=True, exist_ok=True)
    users = Users(str(base('front') / 'accounts.db'), {
        'manager': Group(can=frozenset({'superuser'})),
        'console': Group(can=frozenset({'follow-settings', 'sign-in', 'mirror-users'})),
    })
    users.add('operator', 'manager')
    users.add('weightroom', 'console')
    private_file(base('front') / 'console.key', users.new_key('weightroom'))
    private_file(base('front') / 'test-session.key', users.new_key('operator', hours=24))
    for name in ('compute', 'rig'):
        key = secrets.token_urlsafe(40)
        private_file(base('front') / (name + '.key'), key)
        private_file(base(name) / 'machine.key', key)
    image = base('front') / 'storage/image'
    tensor_file(image / 'sdxl/checkpoint.safetensors', {
        'model.diffusion_model.label_emb.0.0.weight': (1280, 2816),
        'conditioner.embedders.1.model.ln_final.weight': (1280,),
        'first_stage_model.decoder.conv_in.weight': (512, 4, 3, 3),
    })
    tensor_file(image / 'qwen/diffusion.safetensors', {
        'img_in.weight': (4096, 64), 'transformer_blocks.0.img_mod.1.weight': (12288, 4096),
    }, b'OWG image transfer fixture\n' * 131072)
    tensor_file(image / 'concurrent/diffusion.safetensors', {
        'img_in.weight': (4096, 64), 'transformer_blocks.0.img_mod.1.weight': (12288, 4096),
    }, b'OWG concurrent transfer fixture\n' * 131072)
    tensor_file(image / 'qwen/encoder.safetensors', {
        'model.language_model.embed_tokens.weight': (152064, 3584),
    })
    tensor_file(image / 'qwen/vae.safetensors', {'decoder.conv_in.weight': (512, 16, 3, 3, 3)})
    tensor_file(image / 'adapters/style.safetensors', {'transformer_blocks.0.attn.to_q.lora_B.weight': (4096, 32)})
    (image / 'upscalers').mkdir(exist_ok=True)
    (image / 'upscalers/4x.pth').write_bytes(b'qualification upscaler fixture; never unpickle')


def application(role, name):
    if role == 'turnstile':
        from turnstile.app import Settings, build_app
        return build_app(Settings(compute='compute', data=str(base('front')), host='0.0.0.0',
            machine_key_file=str(base('front') / 'compute.key'),
            auth_key_file=str(base('front') / 'console.key'), machines_poll_seconds=1,
            models_folder=str(base('front') / 'cache'), models_storage_folder=str(base('front') / 'storage'),
            machines={'rig': {'compute': 'http://rig:8442', 'spotter': 'http://rig:8444',
                              'machine_key_file': str(base('front') / 'rig.key')}},
        )), 8442
    if role == 'console':
        from weightroom.app import Settings, build_app
        return build_app(Settings(compute='http://compute:8442', door='http://turnstile',
            key=(base('front') / 'console.key').read_text(), data=str(base('front')),
            spotter='http://compute-files:8444', host='0.0.0.0', secure=False)), 8440
    if role == 'loadcoach':
        from loadcoach.app import build_app
        from loadcoach.config import Config
        # /bin/true marks an installed engine for installation-only tests. Never generate with fixtures.
        return build_app(Config(machine_name=name, machine_key_file=str(base(name) / 'machine.key'),
            front='http://turnstile:8442', database=str(base(name) / 'loadcoach.db'),
            token_file=str(base(name) / 'loadcoach.token'), data=str(base(name)), host='0.0.0.0',
            places=places(name), engines={'sdcpp': {'enabled': True, 'executable': '/bin/true', 'cli': '/bin/true'}},
            nvidia_smi='no-gpu-in-qualification',
        )), 8442
    if role == 'spotter':
        from spotter.app import Settings, build_app
        return build_app(Settings(services={}, compute=f'http://{name}:8442', host='0.0.0.0',
            machine_key_file=str(base(name) / 'machine.key'), places=places(name),
            token_file=str(base(name) / 'loadcoach.token'), database=str(base(name) / 'loadcoach.db'),
            data=str(base(name)), history=str(base(name) / 'spotter.db'),
        )), 8444
    raise ValueError('Unknown qualification service')


if __name__ == '__main__':
    role = sys.argv[1]
    if role == 'seed':
        seed()
    else:
        app, port = application(role, sys.argv[2] if len(sys.argv) > 2 else '')
        if role == 'turnstile':
            from turnstile.app import listening
            uvicorn.Server(uvicorn.Config(app, access_log=False)).run(
                sockets=[listening('0.0.0.0', one) for one in (8442, 8443, 8444)])
        else:
            uvicorn.run(app, host='0.0.0.0', port=port, access_log=False)
