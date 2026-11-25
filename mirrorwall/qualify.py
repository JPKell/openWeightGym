"""The run that re-qualifies an image or video profile, and the evidence kept from its output.

An automatic qualification sends one ordinary request to the model's own runtime: a short video
clip at the profile's defaults, or an image at the largest configured envelope (see
telemetry.envelope). Its output is not kept as a library item. Only its SHA-256 and a few small
JPEG frames stay with the model's evidence, so an operator can see what the run produced.
"""
import base64
import hashlib
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from mirrorwall.video import is_video

VIDEO_PROMPT = 'A red boat gently moving across a calm lake, natural daylight.'
IMAGE_PROMPT = 'A red boat on a calm lake at midday, natural daylight, detailed.'
FRAMES = 3
THUMBNAIL = 160


def request(name, settings):
    """(path, body) of the run that qualifies this profile, as the runtime child accepts it."""
    if is_video(settings):
        from mirrorwall import video
        normal, most = video.defaults(settings), video.limits(settings)
        return '/v1/videos/generations', {'model': name, 'prompt': VIDEO_PROMPT, 'seconds': min(2.1, most['max_seconds']),
                                          'width': normal['width'], 'height': normal['height'], 'seed': 1}
    from mirrorwall import model
    most = model.limits(settings)
    references = [grey()] * most['max_references']
    return '/v1/images/generations', {'model': name, 'prompt': IMAGE_PROMPT, 'width': most['max_width'], 'height': most['max_height'],
                                      'upscale': most['max_upscale'], 'seed': 1, 'n': 1,
                                      **({'references': references} if references else {})}


def grey():
    """A plain reference image; only its presence and size matter to the envelope."""
    output = io.BytesIO()
    Image.new('RGB', (512, 512), (128, 128, 128)).save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode()


def thumbnail(image):
    """A small JPEG of one frame, base64."""
    image = image.convert('RGB')
    image.thumbnail((THUMBNAIL, THUMBNAIL))
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=70)
    return base64.b64encode(output.getvalue()).decode()


def video_frames(content, count=FRAMES):
    """Evenly spaced frames of a WebM, decoded by ffmpeg; none if it cannot be read."""
    with tempfile.TemporaryDirectory(prefix='suite-qualify-') as folder:
        source = Path(folder) / 'clip.webm'
        source.write_bytes(content)
        try:
            answer = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                     'stream=nb_read_frames', '-of', 'csv=p=0', str(source)], capture_output=True, text=True, timeout=60, check=True)
            total = int(answer.stdout.strip().split(',')[0])
            picks = sorted({round(index * (total - 1) / max(1, count - 1)) for index in range(count)}) if total > 0 else []
            frames = []
            for pick in picks:
                target = Path(folder) / f'{pick}.png'
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source), '-vf', f'select=eq(n\\,{pick})', '-frames:v', '1', str(target)],
                               capture_output=True, timeout=60, check=True)
                with Image.open(target) as image:
                    frames.append(thumbnail(image))
            return frames
        except (OSError, ValueError, subprocess.SubprocessError):
            return []


def sample(answer, video):
    """The kept evidence of a finished run: the output's SHA-256 and a few small frames."""
    data = (answer.get('data') or [{}])[0] if isinstance(answer.get('data'), list) else answer.get('data') or answer
    content = base64.b64decode(data.get('b64_json', ''), validate=True)
    if not content:
        raise ValueError('The qualification run returned no output.')
    if video:
        frames = video_frames(content)
    else:
        with Image.open(io.BytesIO(content)) as image:
            frames = [thumbnail(image)]
    return {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content), 'frames': frames}
