"""Render three self-contained image-extraction proposals with prepared, tab-local samples."""

import argparse
import base64
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def drawing(width, height, elements):
    """Code-native sample documents; the source and fixture text are authored together."""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{elements}</svg>'
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


def text(x, y, value, size=20, weight='normal', family='monospace'):
    """Place escaped fixture text in its authored SVG document."""
    return f'<text x="{x}" y="{y}" xml:space="preserve" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="#27302e">{escape(value)}</text>'


def samples():
    """Build synthetic source documents with matching tab-local result fixtures."""
    receipt_lines = [
        (48, 90, 'CEDAR MARKET', 38), (48, 130, '18 Oak Street, Vancouver', 21),
        (48, 180, '2026-11-13   15:42', 23), (48, 235, 'RECEIPT 001 · CAD', 23),
        (48, 300, 'Coffee       2 x 4.50    9.00', 25),
        (48, 345, 'Pastry       1 x 5.40    5.40', 25),
        (48, 390, 'Tea          1 x 8.00    8.00', 25),
        (48, 480, 'Subtotal              22.40', 26),
        (48, 525, 'Tax                    1.?2', 26),
        (48, 585, 'TOTAL                 23.52', 32),
        (48, 665, 'VISA ···· 1842', 24), (48, 735, 'Thank you. See you again.', 22),
    ]
    receipt_art = '<rect width="600" height="800" fill="#fffdf5"/><path d="M48 260H552 M48 430H552 M48 620H552" stroke="#b3b6ab" stroke-dasharray="6 5"/>'
    receipt_art += ''.join(text(x, y, value, size) for x, y, value, size in receipt_lines)
    receipt_text = '\n'.join(row[2].replace('1.?2', '[unclear]') for row in receipt_lines)
    document_art = '<rect width="800" height="900" fill="#fffefb"/><path d="M400 200V570 M50 175H750 M50 620H750" stroke="#ccd1c8"/>'
    document_lines = [
        (50, 70, 'FIELD NOTES / 07', 18), (50, 125, 'A quieter workspace', 37),
        (50, 215, '01  LIGHT', 23), (50, 258, 'Face the window.', 21),
        (50, 292, 'Keep glare off screens.', 21), (50, 357, '02  SOUND', 23),
        (50, 400, 'Use soft surfaces.', 21), (50, 434, 'Leave room for focus.', 21),
        (430, 215, '03  SPACE', 23), (430, 258, 'Keep tools within reach.', 21),
        (430, 292, 'Give each task a home.', 21), (430, 357, '04  RHYTHM', 23),
        (430, 400, 'Take a short break.', 21), (430, 434, 'Return with fresh eyes.', 21),
        (50, 585, 'SETUP CHECKLIST', 23), (50, 675, 'Component     Qty    Status', 25),
        (50, 725, 'Sensor          4    Ready', 25), (50, 775, 'Hub             1    Check', 25),
        (50, 825, 'Cable           8    Ready', 25),
    ]
    document_art += ''.join(text(*row) for row in document_lines)
    document_text = '\n'.join(row[2] for row in document_lines)
    document_layout = '\n'.join([
        'FIELD NOTES / 07', 'A quieter workspace', '',
        '01  LIGHT                       03  SPACE',
        'Face the window.                Keep tools within reach.',
        'Keep glare off screens.         Give each task a home.', '',
        '02  SOUND                       04  RHYTHM',
        'Use soft surfaces.              Take a short break.',
        'Leave room for focus.           Return with fresh eyes.', '',
        'SETUP CHECKLIST', 'Component     Qty    Status',
        'Sensor          4    Ready', 'Hub             1    Check', 'Cable           8    Ready',
    ])
    scene_art = '''<rect width="800" height="600" fill="#e6ebe4"/>
<rect x="450" y="40" width="290" height="270" rx="8" fill="#f9fbef"/>
<path d="M595 40V310 M450 175H740" stroke="#c9d4c2" stroke-width="10"/>
<path d="M100 430H730L680 565H55Z" fill="#b79374"/><path d="M100 430H730V455H90Z" fill="#8e715c"/>
<rect x="280" y="383" width="170" height="90" rx="5" fill="#f8f4e5" transform="rotate(-8 350 425)"/>
<path d="M350 385L363 468" stroke="#a49c89" stroke-width="2"/>
<ellipse cx="550" cy="425" rx="43" ry="12" fill="#6e7e70"/>
<path d="M550 420V260L495 218" stroke="#4a5b52" stroke-width="10" fill="none"/>
<path d="M458 230Q488 166 535 202L520 250Z" fill="#667b68"/>
<path d="M141 390H233L216 464H157Z" fill="#ad7053"/>
<path d="M185 390V260 M185 330Q120 270 145 244Q198 257 185 330 M185 350Q256 280 242 258Q190 273 185 350" fill="#648262" stroke="#496c49" stroke-width="5"/>
<path d="M135 575H695" stroke="#d2d9cd" stroke-width="3"/>'''
    receipt = dict(merchant='Cedar Market', date='2026-11-13', currency='CAD', subtotal=22.40,
                   tax=None, total=23.52, items=[dict(description='Coffee', quantity=2, amount=9.0),
                                              dict(description='Pastry', quantity=1, amount=5.4),
                                              dict(description='Tea', quantity=1, amount=8.0)])
    return {
        'receipt': dict(name='cedar-market.png', width=600, height=800, task='receipt', format='json',
                        image=drawing(600, 800, receipt_art), text=receipt_text, layout=receipt_text,
                        description='A Cedar Market receipt with three line items and a total of CAD 23.52. The tax amount is partly unreadable.',
                        detailed='A cream paper receipt from Cedar Market, 18 Oak Street, Vancouver, dated November 13, 2026 at 15:42. It lists two coffees, a pastry and tea. The subtotal is CAD 22.40 and the total is CAD 23.52. Tax is partly unreadable; payment shows Visa ending 1842.',
                        receipt=receipt, table=[], regions=[
                            dict(field='merchant', box=[7, 6, 84, 7]), dict(field='date', box=[7, 19, 75, 5]),
                            dict(field='currency', box=[7, 26, 80, 5]), dict(field='subtotal', box=[7, 56, 86, 5]),
                            dict(field='tax', box=[7, 62, 86, 5]), dict(field='total', box=[7, 69, 86, 6])]),
        'document': dict(name='workspace-notes.png', width=800, height=900, task='text', format='layout',
                         image=drawing(800, 900, document_art), text=document_text, layout=document_layout,
                         description='A two-column guide to light, sound, space and rhythm in a workspace, followed by a setup checklist.',
                         detailed='An editorial document titled A quieter workspace. Two columns contain four sections: Light and Sound on the left, Space and Rhythm on the right. Below the columns, a checklist table lists sensors, a hub and cables, with quantities and readiness states.',
                         receipt=None, table=[['Component', 'Qty', 'Status'], ['Sensor', 4, 'Ready'], ['Hub', 1, 'Check'], ['Cable', 8, 'Ready']],
                         regions=[dict(field='Left column', box=[5, 21, 44, 31]), dict(field='Right column', box=[53, 21, 42, 31]), dict(field='Table', box=[5, 69, 90, 25])]),
        'scene': dict(name='studio-corner.svg', width=800, height=600, task='describe', format='markdown',
                      image=drawing(800, 600, scene_art), text='', layout='',
                      description='An illustration of a wooden desk holding an open notebook, a green lamp and a potted plant beside a window.',
                      detailed='A quiet workspace illustration in muted green, cream and brown. A potted plant sits on the left of a wooden desk, an open notebook lies in the centre, and an angled green desk lamp stands on the right. A large divided window fills the upper right background. There is no visible text.',
                      receipt=None, table=[], regions=[]),
    }


def render():
    """Render all layouts and assets into one offline HTML preview."""
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    return pages.get_template('extraction_designs.html').render(
        css='\n'.join((ROOT / 'weightroom/static' / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'extraction-designs.css')),
        theme_javascript=(ROOT / 'weightroom/static/themes.js').read_text(),
        javascript=(ROOT / 'weightroom/static/extraction-designs.js').read_text(), samples=samples())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/extraction-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
