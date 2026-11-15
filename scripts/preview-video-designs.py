"""Render six offline video proposals using the shared studio and model setup controls."""
import argparse
import base64
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render():
    """Build a portable sample page without contacting models or external services."""
    from mirrorwall.model import SAMPLERS, SCHEDULERS
    pages = Environment(loader=FileSystemLoader([ROOT/'weightroom/pages', ROOT/'mirrorwall/pages']),
                        autoescape=select_autoescape())
    pages.filters['node_label']=str
    static = ROOT/'weightroom/static'
    poster = 'data:image/webp;base64,'+base64.b64encode((static/'generation-samples/tiger.webp').read_bytes()).decode()
    samples = [
        {'id':'wan-small','family':'wan2.1','label':'WAN 2.1 · T2V 1.3B','features':[], 'ready':True},
        {'id':'wan-pair','family':'wan2.2','label':'WAN 2.2 · A14B','features':['init_image','end_image','high_noise','vace'],'ready':True},
        {'id':'ltx','family':'ltx2','label':'LTX-2.3 · 22B','features':['init_image','end_image','soundtrack','hires'],'ready':True},
        {'id':'hunyuan','family':'hunyuan-video-1.5','label':'HunyuanVideo 1.5','features':[],'ready':False},
        {'id':'minimax','family':'minimax-h3','label':'MiniMax-H3','features':['init_image','end_image','ref_images','soundtrack'],'ready':False},
    ]
    return pages.get_template('video_designs.html').render(
        css='\n'.join((static/name).read_text() for name in ('tabler.min.css','style.css','themes.css','generation.css','image-studio.css'))+'\n'+(ROOT/'mirrorwall/static/video.css').read_text(),
        javascript=(ROOT/'mirrorwall/static/video-designs.js').read_text(), samplers=SAMPLERS,
        schedulers=SCHEDULERS, samples={'poster':poster}, sample_data={'models':samples,'poster':poster})


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'build/video-designs/index.html')
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
