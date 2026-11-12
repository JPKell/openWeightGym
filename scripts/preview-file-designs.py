"""Render three compact individual-file proposals with browser-local sample operations."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    pages.filters['shown'] = lambda value: str(value)
    facts = dict(name='Qwen3 32B', size_label='32B', architecture='qwen3', layers=64,
                 trained_context=40960, license='apache-2.0', chat_template=True,
                 takes_tools=True, thinking_switch=True)
    metadata = {
        'general.name': 'Qwen3 32B', 'general.architecture': 'qwen3',
        'general.size_label': '32B', 'general.file_type': 'Q6_K',
        'general.quantization_version': 2, 'general.license': 'apache-2.0',
        'general.type': 'model', 'qwen3.block_count': 64, 'qwen3.context_length': 40960,
        'qwen3.embedding_length': 5120, 'qwen3.feed_forward_length': 25600,
        'qwen3.attention.head_count': 64, 'qwen3.attention.head_count_kv': 8,
        'qwen3.attention.layer_norm_rms_epsilon': 0.000001,
        'qwen3.rope.freq_base': 1000000, 'tokenizer.ggml.model': 'gpt2',
        'tokenizer.ggml.pre': 'qwen2', 'tokenizer.ggml.bos_token_id': 151643,
        'tokenizer.ggml.eos_token_id': 151645, 'tokenizer.ggml.padding_token_id': 151643,
        'tokenizer.ggml.add_bos_token': False,
        'tokenizer.chat_template': "{% if enable_thinking is defined %}…{% endif %} <|im_start|>{{ message.role }} … <|im_end|> · tools supported",
    }
    return pages.get_template('file_designs.html').render(
        css='\n'.join((ROOT / 'weightroom/static' / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'file.css', 'file-designs.css')),
        javascript=(ROOT / 'weightroom/static/file-designs.js').read_text(),
        theme_javascript=(ROOT / 'weightroom/static/themes.js').read_text(),
        told=dict(facts=facts, metadata=metadata), unread='', model=None,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/file-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
