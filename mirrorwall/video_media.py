"""Optional bounded control-node video derivatives; no shell or network media inputs."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def prepare(content, metadata, *, remux=True):
    """Copy the video stream, strip disabled sound or remux PCM to Opus, and make a poster."""
    if not content.startswith(b'\x1aE\xdf\xa3'):
        raise ValueError('Expected a WebM container.')
    ffmpeg=shutil.which('ffmpeg')
    ffprobe=shutil.which('ffprobe')
    sound=metadata.get('settings',{}).get('soundtrack',False)
    result=dict(metadata)
    if not ffmpeg:
        if not sound and metadata.get('soundtrack_present'):
            raise ValueError('Disabling this soundtrack requires ffmpeg on the control node.')
        result['playback_note']='ffmpeg unavailable: browser creates the poster; native PCM soundtrack may need a compatible player.'
        return content,result
    with tempfile.TemporaryDirectory(prefix='suite-video-media-') as temporary:
        root=Path(temporary)
        source=root/'source.webm'
        source.write_bytes(content)
        audio=False
        if ffprobe:
            answer=subprocess.run([ffprobe,'-v','error','-show_streams','-of','json',str(source)],
                                  capture_output=True,timeout=30,check=True)
            streams=json.loads(answer.stdout).get('streams',[])
            videos=[row for row in streams if row.get('codec_type')=='video']
            if len(videos)!=1 or videos[0].get('width')!=metadata['width'] or videos[0].get('height')!=metadata['height']:
                raise ValueError('Encoded video dimensions differ from the admitted profile.')
            audio=any(row.get('codec_type')=='audio' for row in streams)
        else:
            audio=bool(metadata.get('soundtrack_present'))
        # Native WebM omits the final packet duration. A stream copy restores it and seeking.
        if not audio or remux or not sound:
            output=root/'output.webm'
            command=[ffmpeg,'-nostdin','-v','error','-i',str(source),'-map','0:v:0','-c:v','copy']
            command += ['-map','0:a?','-c:a','libopus'] if audio and sound else ['-an']
            subprocess.run([*command,'-map_metadata','-1','-fs',str(512*2**20),str(output)],
                           capture_output=True,check=True,timeout=120)
            content=output.read_bytes()
            result['audio_codec']='opus' if audio and sound else None
        elif audio:
            result['playback_note']='Native PCM soundtrack; playback depends on the browser. Enable soundtrack remux for Opus.'
        poster=root/'poster.png'
        subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(source),'-frames:v','1',
                        '-vf',"scale=384:384:force_original_aspect_ratio=decrease",str(poster)],
                       capture_output=True,check=True,timeout=30)
        result['poster_b64']=base64.b64encode(poster.read_bytes()).decode()
        result['soundtrack_present']=audio and sound
    return content,result
