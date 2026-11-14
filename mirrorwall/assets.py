"""Image bundle discovery without loading checkpoints or executing pickle payloads."""
from pathlib import Path

EXTENSIONS = ('.gguf', '.safetensors', '.pth')


def supported(path):
    """Recognize model data formats, never partial downloads or arbitrary scripts."""
    if Path(path).name.startswith('.suite-package-'):
        return False
    if 'classifier' in Path(path).parts:
        return Path(path).suffix.lower() in ('.gguf', '.safetensors', '.json', '.model', '.txt', '.jinja') or Path(path).name == 'Modelfile'
    audio_data = ('.json', '.bin', '.onnx', '.npz', '.npy', '.model', '.txt', '.tiktoken', '.pt', '.vocab', '.merges')
    tokenizer_files = {'vocab.json', 'merges.txt', 'token_type.json', 'chat_template.jinja'}
    return Path(path).name in tokenizer_files or str(path).lower().endswith(EXTENSIONS + (audio_data if set(Path(path).parts) & {'audio', 'encoders'} else ()))


def image_asset(path):
    """Identify image-library assets while preserving ordinary GGUF inspection."""
    file = Path(path)
    if 'classifier' in file.parts:
        return False
    return file.suffix.lower() in ('.safetensors', '.pth') or 'image' in file.parts or ('encoders' in file.parts and _image_encoder(file))


def _image_encoder(file):
    """Use tensor metadata, not the new folder name, for detached image encoders."""
    from mirrorwall.components import identify
    from engines.llama_cpp.gguf import read_metadata
    if file.suffix.lower()=='.gguf':
        metadata=read_metadata(str(file))
        architecture=metadata.get('general.architecture')
        if architecture and architecture+'.pooling_type' in metadata:
            return False
    return identify(str(file)).get('kind') in ('text_encoder', 'clip_l', 'clip_g')


def facts(path):
    """Describe roles from library placement; format support is qualified at model load."""
    file = Path(path)
    role = next((part for part in ('diffusion','text_encoders','vae','upscalers','loras') if part in file.parts), 'image component')
    from mirrorwall.components import identify, IDENTITY_VERSION
    identity = {**identify(str(file)), 'version': IDENTITY_VERSION}
    if identity.get('kind') not in ('unknown','unreadable','missing'):
        role = identity['kind']
    if file.suffix.lower()=='.gguf' and role=='text_encoder':
        from engines.llama_cpp.gguf import read_metadata, facts as gguf_facts
        metadata=read_metadata(str(file))
        if metadata.get('general.architecture') and metadata.get('tokenizer.ggml.model'):
            return {**gguf_facts(metadata),'image_component':role,'format':'gguf','image_identity':identity}
    return {'what':'image', 'name':file.name, 'kind':role, 'format':file.suffix.lstrip('.'), 'image_identity':identity}
