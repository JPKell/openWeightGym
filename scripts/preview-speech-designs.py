"""Render three self-contained Speech proposals with browser-local interactions."""

import argparse
import re
import tomllib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    families = tomllib.loads((ROOT / 'soundroom/catalog.toml').read_text())['families']
    return dict(
        models=[dict(id=identifier, name=name, family=family, kind=kind,
                     licence=families[family]['licence'], node=node)
                for identifier, name, family, kind, node in [
                    ('qwen-speech', 'Qwen3 · Built-in voices', 'qwen3_tts', 'speech', 'atlas'),
                    ('qwen-clone', 'Qwen3 · Saved voices', 'qwen3_tts', 'clone', 'atlas'),
                    ('qwen-design', 'Qwen3 · Voice design', 'qwen3_tts', 'design', 'cedar'),
                    ('chatterbox', 'Chatterbox · Saved voices', 'chatterbox', 'clone', 'nova')]],
        voices=[dict(id='juniper', name='Juniper', initials='JU', description='Warm · measured · intimate', note='For stories that deserve a little space.', source='Your voice'),
                dict(id='ellis', name='Ellis', initials='EL', description='Clear · grounded · conversational', note='An easy, natural presence.', source='Shared with your group'),
                dict(id='rowan', name='Rowan', initials='RO', description='Bright · textured · expressive', note='A little character in every sentence.', source='Your voice')],
        scripts=dict(narration='Some ideas start with a little space. A quiet room. An open window. A moment to hear yourself think.\n\nMake room for what comes next.',
                     dialogue='Maya: What if we started with something simple?\nAlex: A little space. A clear idea.\nMaya: And a voice that makes it feel human.\nAlex: That sounds like a good place to begin.',
                     announcement='Welcome to the next chapter. Your workspace is ready. Take a breath, settle in, and make something that matters.'),
        takes=[dict(id='sample-1', title='A little space', voice='Juniper', model='Qwen3 · Saved voices', duration='0:14', time='Just now', favourite=True, script='narration'),
               dict(id='sample-2', title='A conversation, naturally', voice='Ellis + Rowan', model='Chatterbox · Saved voices', duration='0:18', time='8 minutes ago', favourite=False, script='dialogue'),
               dict(id='sample-3', title='The next chapter', voice='Rowan', model='Qwen3 · Saved voices', duration='0:09', time='22 minutes ago', favourite=False, script='announcement')])


def render():
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    return pages.get_template('speech_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'speech.css', 'speech-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'speech-designs.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="speech-icon-\1"', (static / 'icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#speech-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/speech-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
