"""Render three Music generation proposals with playable, original synth demos."""

import argparse
import base64
import io
import math
import re
import struct
import tomllib
import wave
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def demo_audio(index):
    """An original sixteen-second arpeggio, authored here; never a model output."""
    rate, seconds = 16000, 16
    chords = [(57, 60, 64, 67), (53, 57, 60, 64), (48, 52, 55, 59), (55, 59, 62, 65)]
    pcm = bytearray()
    for frame in range(rate * seconds):
        t = frame / rate
        beat = t * 2
        chord = chords[int(t // 4) % 4]
        note = chord[(int(beat * 2) + index) % 4] + 12
        frequency = 440 * 2 ** ((note - 69) / 12)
        envelope = math.exp(-7 * ((beat * 2) % 1))
        lead = (math.sin(2 * math.pi * frequency * t) + .2 * math.sin(4 * math.pi * frequency * t)) * envelope
        bass = math.sin(2 * math.pi * 440 * 2 ** ((chord[0] - 81) / 12) * t) * .45
        kick_at = beat % 1
        kick = math.sin(2 * math.pi * (48 * kick_at + 6 * (1 - math.exp(-25 * kick_at)))) * math.exp(-18 * kick_at)
        fade = min(1, t * 5, (seconds - t) * 3)
        pcm.extend(struct.pack('<h', int((lead * .25 + bass * .3 + kick * .22) * fade * 32767)))
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return 'data:audio/wav;base64,' + base64.b64encode(buffer.getvalue()).decode()


def sample_data():
    families = tomllib.loads((ROOT / 'soundroom/catalog.toml').read_text())['families']
    return dict(models=[dict(id=id_, family=family, **families[family]) for id_, family in [
        ('ACE-Step Studio', 'ace_step'), ('HeartMuLa Songs', 'heartmula'),
        ('Stable Audio', 'stable_audio'), ('YuE 2', 'yue2')]],
        audio=[demo_audio(i) for i in range(3)])


def render():
    pages = Environment(loader=FileSystemLoader([ROOT / 'weightroom/pages', ROOT / 'soundroom/pages']),
                        autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('music_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'music.css', 'music-designs.css')),
        javascript=(static / 'music-designs.js').read_text(),
        theme_javascript=(static / 'themes.js').read_text(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="music-icon-\1"', (static / 'icons.svg').read_text()),
        data=sample_data(), mode='music', audio={'lyrics': 'chat'},
    ).replace('/static/icons.svg?v=3#', '#music-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/music-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
