"""Civitai metadata and verified downloads through openWeightGym's existing download manager."""
import asyncio
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from loadcoach import hub

BASE='https://civitai.com'


async def get(http,path,token='',**params):
    """Never put the private token in a URL or expose provider response bodies."""
    try:
        answer=await http.get(BASE+path,headers=hub.signed(token),params=params,timeout=30)
    except Exception as problem:
        raise hub.Refused(502,'Civitai metadata is unavailable: '+type(problem).__name__) from None
    if answer.status_code>=400:
        raise hub.Refused(403 if answer.status_code in (401,403) else 404 if answer.status_code==404 else 502,
                          'Civitai access failed ('+str(answer.status_code)+'); check availability and the administrator token.')
    return answer.json()


def preview(one,adult=False):
    """Require an explicitly safe classification unless adult previews are enabled."""
    classified=one.get('nsfw') in (False,'None') or one.get('nsfwLevel')==1
    address=one.get('url','')
    parsed=urlsplit(address)
    if (adult or classified) and parsed.scheme=='https' and parsed.hostname=='image.civitai.com':
        return address
    return None


async def search(http,words,token='',adult=False):
    """Search bounded model versions, retaining compatibility and source attribution."""
    value=await get(http,'/api/v1/models',token,query=words[:200],limit=20,nsfw='true' if adult else 'false',sort='Most Downloaded')
    found=[]
    for model in value.get('items',[]):
        if model.get('nsfw') and not adult:
            continue
        for version in model.get('modelVersions',[])[:5]:
            found.append({'repository':'civitai/'+str(version['id']),'name':model.get('name','')+' · '+version.get('name',''),
                          'base_model':version.get('baseModel',''),'kind':model.get('type',''),
                          'trigger_words':version.get('trainedWords',[]),'preview':next((url for one in version.get('images',[]) if (url:=preview(one,adult))),None),
                          'url':BASE+'/models/'+str(model['id'])+'?modelVersionId='+str(version['id']),
                          'license':{key:model.get(key) for key in ('allowNoCredit','allowCommercialUse','allowDerivatives','allowDifferentLicense')}})
    return found


async def files(http,repository,token=''):
    """Allow safe native asset names with SHA-256; never offer executables/pickle loaders."""
    version=repository.removeprefix('civitai/')
    if not version.isdigit():
        raise hub.Refused(400,'Choose a numeric Civitai model-version ID.')
    value=await get(http,'/api/v1/model-versions/'+version,token)
    result=[]
    for file in value.get('files',[]):
        name=file.get('name','')
        sha=file.get('hashes',{}).get('SHA256','').lower()
        address=file.get('downloadUrl','')
        parsed=urlsplit(address)
        if Path(name).name!=name or '\\' in name or not name.lower().endswith(('.gguf','.safetensors','.pth')):
            continue
        if not re.fullmatch('[a-f0-9]{64}',sha):
            continue
        if parsed.scheme!='https' or parsed.hostname!='civitai.com' or parsed.path!='/api/download/models/'+version:
            continue
        # The provider's sizeKB is rounded; final identity is the mandatory SHA-256.
        size=math.ceil(float(file.get('sizeKB',0))*1024)
        if not 0<size<2**40:
            continue
        result.append({'path':name,'size':size,'sha256':sha,'download_url':address,'version':int(version),
                       'base_model':value.get('baseModel'),'trigger_words':value.get('trainedWords',[])})
    return result


async def fetch(http,repository,entry,target,token,progress):
    """Resume bounded bytes, verify provider SHA and publish atomically without overwrite."""
    part=target.with_name(target.name+'.part')
    have=part.stat().st_size if part.exists() else 0
    if have>entry['size']+1024:
        have=0
    progress.update(done=have,start=have,since=time.monotonic())
    headers={**hub.signed(token),**({'range':f'bytes={have}-'} if have else {})}
    try:
        async with http.stream('GET',entry['download_url'],headers=headers,follow_redirects=True,timeout=120) as response:
            if response.status_code>=400:
                raise hub.Refused(403 if response.status_code in (401,403) else 502,'Civitai download is unavailable ('+str(response.status_code)+').')
            if response.status_code!=206:
                have=0
                progress['done']=progress['start']=0
            with part.open('ab' if have else 'wb') as stream:
                os.chmod(part,0o600)
                async for chunk in response.aiter_bytes(2**20):
                    if progress['done']+len(chunk)>entry['size']+1024:
                        raise OSError('Civitai file exceeds its advertised size.')
                    await asyncio.to_thread(stream.write,chunk)
                    progress['done']+=len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        progress['state']='checking'
        actual=await asyncio.to_thread(hub.sha256_of,part)
        if actual!=entry['sha256']:
            part.unlink()
            raise OSError('Civitai SHA-256 verification failed; the download was discarded.')
        os.link(part,target)
        part.unlink()
        source=target.with_name(target.name+'.source.json')
        source.write_text(json.dumps({'source':'civitai','repository':repository,'version':entry['version'],'sha256':actual,
                                     'bytes':target.stat().st_size,'base_model':entry.get('base_model'),'trigger_words':entry.get('trigger_words',[])},indent=2)+'\n')
        source.chmod(0o600)
    except hub.Refused:
        raise
    except Exception as problem:
        if isinstance(problem,OSError):
            raise
        raise hub.Refused(502,'Civitai transfer failed: '+type(problem).__name__) from None
