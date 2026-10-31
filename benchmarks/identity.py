"""Conservative matching of saved model variants; versions and sizes remain in the key."""

import re
from pathlib import Path

QUANT = re.compile(r'(?i)(?:^|[-_.\s])(i?q[1-8](?:[_-](?:k[_-](?:xxs|xs|xl|s|m|l)|k|[0-8]|xxs|xs|s|m|nl))*|bf16|fp16|f16|fp32|f32)(?=$|[.\-])')


def variant(name, subject=None):
    """Return the model variant key used to match compatible benchmark evidence."""
    subject = subject or {}
    path = subject.get('settings', {}).get('flags', {}).get('model', '')
    # The public label keeps base-model versions distinct. A file supplies the quant
    # when the label omits it; no brand-only or parameter-size-only grouping.
    label = str(name).lower().removesuffix('.gguf')
    match = QUANT.search(label)
    file_match = QUANT.search(Path(path).name.removesuffix('.gguf')) if path else None
    quant = (file_match or match).group(1).lower().replace('-', '_') if match or file_match else None
    base = label[:match.start()] if match else label
    if file_match:
        base = Path(path).name[:file_match.start()]
        # UD is a dynamic-quantization marker, not a different base model/version.
        base = re.sub(r'(?i)[-_.]UD$', '', base)
    return {'family': re.sub(r'[-_\s]+', '-', base.lower()).strip('-.'), 'quant': quant}
