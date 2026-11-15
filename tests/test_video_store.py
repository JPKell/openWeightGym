"""Video assets keep stable ownership, quotas and no-log output out of persistent storage."""
import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from loadcoach.batches import Batches
from mirrorwall.store import Store
from mirrorwall.video_media import prepare


@pytest.fixture
def clip(tmp_path):
    """Encode a tiny actual WebM so validation and playback use a real container."""
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg is optional')
    output=tmp_path/'clip.webm'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=256x256:r=16:d=0.3125',
                    '-c:v','libvpx-vp9','-threads','1',str(output)],check=True,capture_output=True,timeout=30)
    return output.read_bytes()


def test_private_video_and_derivatives_are_scoped_and_no_log_is_memory_only(tmp_path,clip):
    store=Store(Batches(str(tmp_path/'accounts.db')).db,tmp_path)
    content,metadata=prepare(clip,{'width':256,'height':256,'seconds':5/16,'settings':{}})
    record=store.save('owner',content,kind='video',metadata=metadata)
    assert store.content('other',record['id']) is None
    assert store.content('owner',record['id'],True).startswith(b'\x89PNG')
    assert 'poster_b64' not in store.asset('owner',record['id'])['metadata']
    assert store.listing('owner',kind='video')[0]['id']==record['id']
    quiet=store.save('owner',content,kind='video',metadata=metadata,temporary=True)
    assert not store.path(quiet['id']).exists()
    assert store.db.execute('SELECT COUNT(*) FROM mirrorwall_assets').fetchone()[0]==1
    assert store.delete('other',record['id']) is False
    assert store.delete('owner',record['id'])


def test_output_cap_quota_and_retention_preserve_existing_files(tmp_path,clip):
    store=Store(Batches(str(tmp_path/'accounts.db')).db,tmp_path)
    metadata={'width':256,'height':256,'seconds':5/16}
    record=store.save('owner',clip,kind='video',metadata=metadata)
    with pytest.raises(ValueError,match='output limit'):
        store.save('owner',clip,kind='video',metadata=metadata,limits={'video_output_max_mib':0})
    with pytest.raises(ValueError,match='quota'):
        store.save('owner',clip,kind='video',metadata=metadata,limits={'video_quota_gib':0})
    assert store.path(record['id']).is_file()
    with store.db:
        store.db.execute('UPDATE mirrorwall_assets SET created=0 WHERE id=?',(record['id'],))
    store.prune_videos('owner',0)
    assert store.asset('owner',record['id'])
    store.prune_videos('other',1)
    assert store.asset('owner',record['id'])
    store.prune_videos('owner',1)
    assert not store.asset('owner',record['id'])


def test_header_detection_and_incompatible_ltx_encoders():
    from mirrorwall.components import classify,needs
    assert classify({'patch_embedding.weight':(1536,16,1,2,2),'blocks.0.cross_attn.norm_k.weight':(1536,)})==('diffusion','wan2.1','t2v-1.3b')
    assert classify({'video_patch_proj.weight':(4096,96),'audio_patch_proj.weight':(4096,128)})==('diffusion','minimax-h3','minimax-h3')
    assert classify({'model.embed_tokens.weight':(262144,3840),'model.layers.0.layer_scalar':(1,)})[2]=='gemma4-stock-unsupported'
    assert needs('ltx2','ltx-2.5')['text_encoder'][2]=='ltx-gemma4-12b'
    assert needs('wan2.2','ti2v-5b')['vae'][2]=='wan2.2-vae'
    assert needs('wan2.2','t2v-14b')['vae'][2]=='wan2.1-vae'


def test_pcm_soundtrack_is_remuxed_to_opus_and_can_be_removed(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe are optional')
    import json
    source=tmp_path/'pcm.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=s=256x256:r=16:d=0.3125',
                    '-f','lavfi','-i','sine=frequency=440:duration=0.3125','-c:v','libvpx-vp9','-threads','1',
                    '-c:a','pcm_s16le',str(source)],check=True,capture_output=True,timeout=30)
    for enabled in (True,False):
        raw,metadata=prepare(source.read_bytes(),{'width':256,'height':256,'seconds':5/16,
                              'soundtrack_present':True,'settings':{'soundtrack':enabled}})
        target=tmp_path/('opus.webm' if enabled else 'silent.webm');target.write_bytes(raw)
        streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(target)]))['streams']
        assert [row['codec_name'] for row in streams if row['codec_type']=='audio']==(['opus'] if enabled else [])
        assert metadata['soundtrack_present'] is enabled


def test_temporary_videos_obey_the_account_quota(tmp_path,clip):
    store=Store(Batches(str(tmp_path/'accounts.db')).db,tmp_path)
    with pytest.raises(ValueError,match='quota'):
        store.save('owner',clip,kind='video',temporary=True,limits={'video_quota_gib':0},
                   metadata={'width':256,'height':256,'seconds':5/16})
    assert not store.temporary and not list(store.root.rglob('*.image'))
