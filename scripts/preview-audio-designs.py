"""Render three Audio models proposals with browser-local sample data."""

import argparse
import re
import tomllib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    families = tomllib.loads((ROOT / 'soundroom/catalog.toml').read_text())['families']
    entries = [
        ('ace-step-studio', 'ACE-Step Studio', 'ace_step', 'music', 6.8, ['atlas', 'nova']),
        ('qwen3-voice', 'Qwen3 Voice', 'qwen3_tts', 'speech', 3.4, ['atlas', 'cedar']),
        ('stable-sound', 'Stable Sound', 'stable_audio', 'sound', 4.2, ['nova']),
        ('parakeet', 'Parakeet Transcribe', 'parakeet_tdt', 'transcription', 1.2, ['cedar']),
        ('chatterbox', 'Chatterbox Clone', 'chatterbox', 'clone', 2.1, ['atlas']),
        ('heartmula', 'HeartMuLa Songs', 'heartmula', 'music', 6.1, ['nova']),
        ('qwen-align', 'Qwen3 Align', 'qwen3_forced_aligner', 'alignment', 1.8, ['cedar']),
        ('sortformer', 'Sortformer Speakers', 'sortformer_diar', 'diarization', 0.5, ['vega']),
    ]
    models = []
    for index, (identifier, name, family, kind, size, nodes) in enumerate(entries):
        file = f'/models/audio/{identifier}/{identifier}.gguf'
        models.append(dict(id=identifier, name=name, family=family, kind=kind, size=size,
            file=file, root=str(Path(file).parent), companions=[], seconds=600, takes=8,
            text=20000, audio=32, defaults='{}', session='{}',
            placements=[dict(node=node, gpu='GPU 0', ram=8192, alone=kind in ('music', 'sound'),
                state='loaded' if index < 2 and ni == 0 else 'installed') for ni, node in enumerate(nodes)]))
    files = [dict(path=model['file'], family=model['family'], tier='cache', node=model['placements'][0]['node']) for model in models]
    files.append(dict(path='/storage/audio/voxcpm2/voxcpm2.gguf', family='voxcpm2', tier='storage', node='vault'))
    return dict(families=families, models=models, files=files, nodes=[
        dict(id='atlas', hardware='2 × RTX 6000 Ada', online=True),
        dict(id='nova', hardware='RTX 5090', online=True),
        dict(id='cedar', hardware='Radeon PRO W7900', online=True),
        dict(id='vega', hardware='RTX 3090', online=False),
        dict(id='scout', hardware='RTX 4070 · no models yet', online=True),
    ])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('audio_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
            ('tabler.min.css', 'style.css', 'themes.css', 'models.css', 'audio-models.css', 'audio-designs.css')),
        javascript=(static / 'audio-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="audio-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#audio-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/audio-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
