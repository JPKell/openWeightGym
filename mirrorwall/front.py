"""Control node image API/assets and existing-batch integration; no inference scheduler."""
import asyncio
import base64
import itertools
import json
import math
import secrets
import time
from urllib.parse import parse_qsl, unquote, urlencode

import httpx
from starlette.responses import JSONResponse,Response,StreamingResponse
from starlette.routing import Route

from loadcoach import observability as observation
from mirrorwall.requests import checked_request,MAX_REQUEST_BYTES
from mirrorwall import graph,graph_run,stream
from mirrorwall.model import SWEEP_FIELDS

IMAGE_PATHS=("/v1/images/generations","/v1/images/edits")
COMPARISON_IMAGES=512


def error(status,message):
    """Return a consistent compatible image error without exposing other owners."""
    return JSONResponse({"error":{"message":message}},status)


class Front:
    """Mirror Wall's control-node-owned assets/presets and ordinary batch execution hooks."""
    def __init__(self,store,batches,users,*,adult_previews_allowed=lambda: False):
        """Initialize private image state without loading model weights."""
        from loadcoach.content_limits import ContentLimits
        self.limits = ContentLimits()
        self.store=store
        self.batches=batches
        self.users=users
        self.adult_previews_allowed=adult_previews_allowed
        self.progress={}
        self.transient={}
        self.graph_runs={}  # test runs from the Image workflows page, in memory for an hour after they end
        self.video_remux=lambda: True

    def adult_previews(self,user):
        """Require both global permission and the current stable owner's opt-in."""
        return bool(user and self.adult_previews_allowed() and self.store.preferences(user.identity).get('adult_previews',False))

    def discovery_path(self,user,path):
        """Apply preview policy before signing any direct or artifact Civitai search."""
        address,_,query=path.partition('?')
        if unquote(address)!='/loadcoach/hub':
            return path
        pairs=parse_qsl(query,keep_blank_values=True)
        if dict(pairs).get('source')!='civitai':
            return path
        return address+'?'+urlencode([(key,value) for key,value in pairs if key!='adult']+
                                    [('adult',str(self.adult_previews(user)).lower())])

    def visible(self,user,batch):
        """Additional stable-identity check for image-owned batch rows."""
        return self.store.visible(user.identity,batch)

    async def catalogue(self,user,kind='image'):
        """Use the authenticated combined catalogue; never reveal forbidden models."""
        answer=await self.batches.door.get('/v1/models',headers={'authorization':'Bearer '+self.batches.internal_key(user.name), 'x-turnstile-no-wake':'1'})
        if answer.status_code!=200:
            raise ValueError("Image model inventory is unavailable.")
        return [one for one in answer.json().get('data',[]) if one.get('capabilities',{}).get(kind+'_generation')]

    async def media_catalogue(self,user,kind):
        """Retain image catalogue callers while selecting the explicit video capability."""
        return await self.catalogue(user) if kind=='image' else await self.catalogue(user,'video')

    def visible_presets(self,user,models):
        """Shared recipes follow immutable model identities when names are reused."""
        return [row for row in self.store.presets(user.identity) if not row['published'] or any(
            row['model_id']==m.get('model_id') if row['model_id'] else row['model']==m['id'] for m in models)]

    def job_owner(self,identifier):
        """Look up either durable or temporary ownership without trusting a request header."""
        return self.transient.get(identifier,{}).get('owner') or self.store.owner(identifier)

    def retain_event(self,user,event,job=None,line=None,quiet=False):
        """Persist completed renders on the control node and hold only the newest partial frame."""
        if job and self.job_owner(job)!=user.identity:
            raise ValueError("There is no such image job of yours.")
        kind=event.get('type')
        if job and kind in ('image_generation.partial_image','image_generation.progress','image_edit.partial_image','image_edit.progress','video_generation.partial_image','video_generation.progress'):
            previous=self.progress.get(job,{}) if kind.startswith('video_generation.') else {}
            self.progress[job]={**previous,**event,**({'preview_b64':event['b64_json']} if kind=='video_generation.partial_image' else {}),'at':time.time()}
        if job and 'error' in event:
            self.progress[job]={**event,'stage':'failed','at':time.time()}
        if kind not in ('image_generation.completed','image_edit.completed','video_generation.completed'):
            return event
        content=base64.b64decode(event['b64_json'],validate=True)
        metadata={k:v for k,v in event.items() if k not in ('b64_json','type')}
        if job:
            metadata.update(job=job,batch_line=line)
        video=kind=='video_generation.completed'
        asset=self.store.save(user.identity,content,kind='video' if video else 'output',filename='generated-'+str(event.get('seed',0))+('.webm' if video else '.png'),
                              metadata=metadata,temporary=quiet,job=job if job and not quiet else None,limits=self.limits.for_user(user))
        if job:
            self.progress[job]={'type':'video_generation.progress' if video else 'image_generation.progress','stage':'completed','at':time.time()}
        return {**event,'asset_id':asset['id'],'asset_url':'/mirrorwall/assets/'+asset['id']+'/content'}

    async def capture(self,request,user,response):
        """Retain allowed completed image responses on the control node while forwarding real previews."""
        if response.status_code>=400:
            return response
        if request.headers.get('x-mirrorwall-intermediate')=='1':
            return response  # a workflow step's image: the run keeps it in memory, and saves only its Outputs
        quiet=request.headers.get('x-loadcoach-log')=='off'
        job=request.headers.get('x-mirrorwall-job')
        line=request.headers.get('x-mirrorwall-line')
        if job and self.job_owner(job)!=user.identity:
            return error(404,"There is no such image job of yours.")
        if quiet:
            observation.silence()
        headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','content-type','content-encoding')}
        headers.update({'cache-control':'no-store','x-suite-image-owner':'front'})
        provenance={key:response.headers['x-loadcoach-'+key.replace('_','-')] for key in ('model_id','fingerprint','build','request_id') if response.headers.get('x-loadcoach-'+key.replace('_','-'))}
        async def chunks():
            """Yield upstream bytes without changing their transport boundaries."""
            if hasattr(response,'body_iterator'):
                async for chunk in response.body_iterator:
                    yield chunk.encode() if isinstance(chunk,str) else chunk
            else:
                yield response.body
        if 'text/event-stream' in response.headers.get('content-type',''):
            async def forwarded():
                """Retain completed private renders while passing live preview events."""
                try:
                    async for event in stream.events(chunks(),max_event=704*2**20 if request.url.path.startswith('/v1/videos/') else stream.MAX_EVENT):
                        if event.get('type')=='video_generation.completed':
                            from mirrorwall.video_media import prepare
                            data,metadata=await asyncio.to_thread(prepare,base64.b64decode(event['b64_json'],validate=True),
                                {key:value for key,value in event.items() if key not in ('b64_json','type')},
                                remux=self.video_remux())
                            event={**event,**metadata,'b64_json':base64.b64encode(data).decode()}
                        yield stream.encode(self.retain_event(user,{**event,**provenance} if event.get('type') in ('image_generation.completed','image_edit.completed','video_generation.completed') else event,job,line,quiet))
                except Exception as problem:
                    yield stream.encode({'type':'error','error':{'message':str(problem)}})
                finally:
                    iterator=getattr(response,'body_iterator',None)
                    if iterator and hasattr(iterator,'aclose'):
                        await iterator.aclose()
            return StreamingResponse(forwarded(),media_type='text/event-stream',headers=headers,background=response.background)
        body=bytearray()
        video=request.url.path.startswith('/v1/videos/')
        async for chunk in chunks():
            body.extend(chunk)
            if len(body)>(704 if video else 256)*2**20:
                return error(502,"Image response exceeds the configured response limit.")
        try:
            decoded=json.loads(body)
            for index,item in enumerate(decoded.get('data',[])):
                if video:
                    from mirrorwall.video_media import prepare
                    data,metadata=await asyncio.to_thread(prepare,base64.b64decode(item['b64_json'],validate=True),
                        {key:value for key,value in item.items() if key!='b64_json'},remux=self.video_remux())
                    item={**item,**metadata,'b64_json':base64.b64encode(data).decode()}
                event=self.retain_event(user,{'type':'video_generation.completed' if video else 'image_generation.completed','image_index':index,**item,**provenance},job,line,quiet)
                item.update({k:v for k,v in event.items() if k not in ('type','image_index')})
                decoded['data'][index]=item
            return JSONResponse(decoded,headers=headers,background=response.background)
        except (ValueError,KeyError,TypeError) as problem:
            return error(502,'Could not retain the generated image: '+str(problem))

    async def execute_line(self,number,line,body,user,headers,*,quiet=False):
        """Execute an existing batch line through the same authenticated image API."""
        if self.job_owner(number)!=user.identity:
            return httpx.Response(403,json={'error':{'message':'Image job owner changed.'}})
        batch=self.batches.store.get(number) if number not in self.transient else None
        video=body.get('video_asset_ids') is not None or bool(batch and batch['endpoint'].startswith('/v1/videos/'))
        if video and 'video_asset_ids' not in body:
            from mirrorwall.video_api import prepare
            from types import SimpleNamespace
            envelope,prepared=await prepare(self,SimpleNamespace(headers={'x-loadcoach-log':'off'} if quiet else {}),user,body)
            body=prepared[0]['body']
            if not quiet:
                with self.store.db:
                    for identifier in set(envelope['references']):
                        self.store.db.execute('INSERT INTO mirrorwall_job_assets VALUES (?,?,?) ON CONFLICT DO NOTHING',(number,identifier,'reference'))
        previous=[asset for asset in self.store.listing(user.identity,kind='video' if video else 'output',job=number) if str(asset['metadata'].get('batch_line'))==str(line['line'])]
        if previous:
            return httpx.Response(200,json={'data':[{'asset_id':asset['id'],**asset['metadata']} for asset in previous]})
        sent=dict(body)
        video_assets=sent.pop('video_asset_ids',{})
        references=sent.pop('reference_asset_ids',[])
        mask=sent.pop('mask_asset_id',None)
        try:
            if references:
                sent['references']=[]
                for identifier in references:
                    content=self.store.content(user.identity,identifier)
                    if content is None:
                        raise ValueError('A reference image was deleted or is unavailable.')
                    sent['references'].append(base64.b64encode(content).decode())
            if mask:
                content=self.store.content(user.identity,mask)
                if content is None:
                    raise ValueError('The mask image is unavailable.')
                sent['mask']=base64.b64encode(content).decode()
            if video:
                from mirrorwall.video import checked_request as check_video
                for key,identifiers in video_assets.items():
                    decoded=[]
                    for identifier in identifiers:
                        content=self.store.content(user.identity,identifier)
                        if content is None:
                            raise ValueError('A video input asset is no longer available.')
                        decoded.append(base64.b64encode(content).decode())
                    sent[key]=decoded if key in ('reference_images','control_video') else decoded[0]
                check_video(sent)
            else:
                checked_request(sent)
        except ValueError as problem:
            return httpx.Response(400,json={'error':{'message':str(problem)}})
        sent['stream']=True
        headers={**headers,'x-mirrorwall-job':number,'x-mirrorwall-line':str(line['line'])}
        if quiet:
            headers={k:v for k,v in headers.items() if k not in ('x-loadcoach-trace','x-loadcoach-priority')}
            headers['x-loadcoach-log']='off'
        result=[]
        request_id=''
        async with self.batches.door.stream('POST','/v1/videos/generations' if video else '/v1/images/edits' if references or mask else '/v1/images/generations',json=sent,headers=headers,timeout=None) as response:
            request_id=response.headers.get('x-loadcoach-request-id','')
            if response.status_code>=400:
                await response.aread()
                return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
            async for event in stream.events(response.aiter_bytes(),max_event=704*2**20 if video else stream.MAX_EVENT):
                if 'error' in event:
                    return httpx.Response(502,json={'error':event['error']})
                if event.get('type') in ('image_generation.completed','image_edit.completed','video_generation.completed'):
                    result.append({k:v for k,v in event.items() if k not in ('type','b64_json')})
        if not result:
            return httpx.Response(502,json={'error':{'message':'The image stream ended without a completed image.'}})
        return httpx.Response(200,json={'created':int(time.time()),'data':result},headers={'x-loadcoach-request-id':request_id})

    async def state(self,request,user):
        """List permitted models, private assets/jobs and versioned workflow presets."""
        for identifier,row in list(self.transient.items()):
            if row['status']!='in_progress' and time.time()-row.get('completed_at',row['created'])>3600:
                self.transient.pop(identifier)
                self.progress.pop(identifier,None)
        try:
            models=await self.catalogue(user)
        except ValueError:
            models=[]
        jobs=[]
        for row in self.store.jobs(user.identity):
            batch=self.batches.store.get(row['id'])
            failure=None
            if batch and batch['request_counts']['failed']:
                for result, in self.store.db.execute('SELECT result FROM batch_lines WHERE batch=? AND result IS NOT NULL ORDER BY line',(row['id'],)):
                    result=json.loads(result)
                    if result['status_code']>=400:
                        failure=result.get('body',{}).get('error',{}).get('message','Image generation failed.')
                        break
            jobs.append({**row,'status':batch['status'] if batch else 'completed',
                         'request_counts':batch.get('request_counts',{}) if batch else {},'progress':{'stage':'queued'} if row['id'] in self.batches.image_waiting else self.progress.get(row['id']),'error':failure})
        for identifier,row in self.transient.items():
            if row['owner']==user.identity:
                jobs.insert(0,{'id':identifier,**{k:v for k,v in row.items() if k not in ('owner','task')},'progress':{'stage':'queued'} if identifier in self.batches.image_waiting else self.progress.get(identifier)})
        return JSONResponse({'models':models,'assets':self.store.listing(user.identity,kind='output'),
             'references':self.store.listing(user.identity,kind='reference'),'presets':self.visible_presets(user,models),
             'jobs':jobs,'preferences':self.store.preferences(user.identity),'can_manage':user.can('manage-models'),'can_unlogged':bool(user.can('unlogged') and await self.batches.can_unlogged(user))},headers={'cache-control':'no-store'})

    async def assets(self,request,user):
        """Page through the caller's retained image library without loading pixels."""
        try:
            kind=request.query_params.get('kind','output')
            if kind not in ('output','reference','video'):
                raise ValueError('Choose outputs or references.')
            before=request.query_params.get('before')
            if before is not None and not math.isfinite(float(before)):
                raise ValueError('Invalid gallery cursor.')
            return JSONResponse({'assets':self.store.listing(user.identity,kind=kind,before=before)},headers={'cache-control':'no-store'})
        except ValueError as problem:
            return error(400,str(problem))

    async def upload(self,request,user):
        """Validate a bounded reference/mask upload and its retention choice."""
        try:
            asked=await self.read_json(request,self.limits.for_user(user))
            temporary=asked.get('temporary',False)
            if type(temporary) is not bool:
                raise ValueError('temporary must be true or false.')
            if temporary and not user.can('unlogged'):
                return error(403,'Your account may not use temporary no-log images.')
            if temporary:
                observation.silence()
            content=base64.b64decode(asked.get('b64_json',''),validate=True)
            asset=self.store.save(user.identity,content,kind=asked.get('kind','reference'),filename=asked.get('filename','reference.png'),temporary=temporary,limits=self.limits.for_user(user))
            return JSONResponse(asset,201,headers={'cache-control':'no-store'})
        except (ValueError,OSError) as problem:
            return error(400,str(problem))

    async def asset(self,request,user):
        """Authorize content, thumbnails, downloads and deletion on every request."""
        identifier=request.path_params['identifier']
        record=self.store.asset(user.identity,identifier)
        if not record:
            return error(404,'There is no such image of yours.')
        if record.get('temporary'):
            observation.silence()
        if request.method=='DELETE':
            if record.get('temporary') and any(row['status']=='in_progress' and (identifier in row.get('references',[]) or record.get('metadata',{}).get('job')==job) for job,row in self.transient.items()):
                return error(409,'This temporary image is used by an unfinished job.')
            try:
                self.store.delete(user.identity,identifier)
            except ValueError as problem:
                return error(409,str(problem))
            return JSONResponse({'deleted':True,'id':identifier})
        content=self.store.content(user.identity,identifier,request.url.path.endswith('/thumbnail'))
        if content is None:
            return error(404,'This image file is unavailable.')
        headers={'cache-control':'no-store','x-content-type-options':'nosniff'}
        if record.get('temporary'):
            headers['x-suite-image-temporary']='1'
        if request.query_params.get('download'):
            suffix='.webm' if record['kind']=='video' else '.png' if content.startswith(b'\x89PNG') else '.jpg' if content.startswith(b'\xff\xd8') else '.webp'
            headers['content-disposition']='attachment; filename="'+identifier+suffix+'"'
        return Response(content,media_type='image/png' if request.url.path.endswith('/thumbnail') or content.startswith(b'\x89PNG') else 'video/webm' if record['kind']=='video' else 'image/jpeg' if content.startswith(b'\xff\xd8') else 'image/webp',headers=headers)

    @staticmethod
    async def read_json(request,limits=None):
        """Read a bounded JSON object without buffering an unlimited upload."""
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>MAX_REQUEST_BYTES:
                raise ValueError('Image request exceeds 60 MiB.')
        value=json.loads(body or b'{}')
        if not isinstance(value,dict):
            raise ValueError('Request must be a JSON object.')
        return value

    def lines(self,asked):
        """Expand bounded image counts/sweeps into existing sequential batch lines."""
        body=dict(asked.get('request',{}))
        references=asked.get('references',[])
        mask=asked.get('mask')
        if not isinstance(references,list) or len(references)>8:
            raise ValueError('Use at most eight reference images.')
        sweep=asked.get('sweep',{})
        if not isinstance(sweep,dict) or sweep.keys()-{key for key,_ in SWEEP_FIELDS}:
            raise ValueError('Unsupported sweep field.')
        if any(not isinstance(values,list) or not values or len(values)>32 or len(set(map(str,values)))!=len(values) for values in sweep.values()):
            raise ValueError('Each sweep requires 1–32 different values.')
        checked_request(body)
        count=body.pop('n',1)
        total=count*math.prod(len(values) for values in sweep.values())
        # A sampler comparison tries every sampler with every scheduler, so it may hold more images than other jobs.
        most=COMPARISON_IMAGES if {'sampler','scheduler'}&sweep.keys() else 128
        if total>most:
            raise ValueError(f'A generation job is limited to {most} images; this one would make {total}.')
        rows=[]
        for combination in itertools.product(*sweep.values()):
            selected={**body,**dict(zip(sweep,combination))}
            for repeat in range(count):
                seed=selected.get('seed',-1)
                values={**selected,'n':1,'stream':True,'seed':(seed+repeat)%(2**31) if seed>=0 else -1}
                checked_request(values)
                if references:
                    values['reference_asset_ids']=references
                if mask:
                    values['mask_asset_id']=mask
                rows.append({'custom_id':'image-'+str(len(rows)+1),'body':values})
        return rows

    def fits(self,selected,asked,user):
        """Refuse before queueing what the model would refuse later: too many references, or small ones it cannot enlarge."""
        workflow=selected.get('image_workflow') or {}
        refs=asked.get('references',[])
        sent=asked.get('request') or {}
        if 'detail_resize' in sent:
            if workflow.get('family')!='sdxl' or not workflow.get('exclusive',False):
                raise ValueError('Detail resize requires an exclusive SDXL image-to-image workflow.')
            if len(refs)!=1:
                raise ValueError('Detail resize requires one source image.')
            target=sent['detail_resize']
            largest=max(target['width']/sent.get('width',1024),target['height']/sent.get('height',1024))
            if largest>1 and not workflow.get('upscaling'):
                raise ValueError('Detail resize needs a configured AI upscaler for the requested final size.')
            if largest>(workflow.get('limits') or {}).get('max_upscale',4 if workflow.get('upscaling') else 1):
                raise ValueError('Detail resize exceeds this workflow upscale limit.')
        most=(workflow.get('limits') or {}).get('max_references',2)
        if len(refs)>most:
            raise ValueError(f"{selected['id']} uses at most {most} reference image{'s' if most!=1 else ''}." if most else f"{selected['id']} does not use reference images.")
        if asked.get('mask') and not refs:
            raise ValueError('An edit mask needs a reference image.')
        if not refs:
            return
        from mirrorwall.runtime import reference_size
        # The detail pass prepares its single source at the requested diffusion size.
        if 'detail_resize' in sent:
            if workflow.get('upscaling'):
                return
            record=self.store.asset(user.identity,refs[0])
            if record['width']*record['height']<sent.get('width',1024)*sent.get('height',1024):
                raise ValueError('The source needs AI enlargement before the detail pass; configure an upscaler or lower its working size.')
            return
        maximum=workflow.get('reference_megapixels',1)
        megapixels=(asked.get('request') or {}).get('reference_megapixels',maximum)
        if type(megapixels) not in (int,float) or not math.isfinite(megapixels) or not .1<=megapixels<=maximum:
            raise ValueError(f'Reference size must be a finite number from 0.1 to {maximum:g} MP.')
        if workflow.get('upscaling'):
            return
        for index,identifier in enumerate(refs):
            record=self.store.asset(user.identity,identifier)
            width,height=reference_size(record['width'],record['height'],megapixels)
            if record['width']*record['height']<width*height:
                raise ValueError(f"Reference {index+1} ({record['width']}×{record['height']}) is smaller than this model's {megapixels:g} MP reference size "
                                 "and the model has no upscaler: use a larger image, lower the reference size, or add an upscaler to the model.")

    async def start(self,request,user):
        """Submit an image job to the established control node batch executor."""
        try:
            asked=await self.read_json(request)
            video=request.url.path=='/v1/videos'
            if video:
                from mirrorwall.video_api import prepare
                asked,lines=await prepare(self,request,user,asked)
            else:
                lines=self.lines(asked)
            models=await self.media_catalogue(user,'video' if video else 'image')
            selected=next((one for one in models if one['id']==lines[0]['body']['model']),None)
            if not selected:
                raise ValueError('Choose an available image model you may use.')
            references=[*asked.get('references',[]),*([asked['mask']] if asked.get('mask') else [])]
            for identifier in references:
                if not self.store.asset(user.identity,identifier):
                    raise ValueError('There is no such reference image of yours.')
            if not video:
                self.fits(selected,asked,user)
            temporary=asked.get('temporary',False)
            if type(temporary) is not bool:
                raise ValueError('temporary must be true or false.')
            if any(self.store.asset(user.identity,key).get('temporary') for key in references) and not temporary:
                raise ValueError('Temporary references require a temporary no-log job.')
            if temporary:
                if not await self.batches.can_unlogged(user):
                    return error(403,'Your account or the current compute policy does not permit no-log generation.')
                observation.silence()
                identifier='quiet_image_'+secrets.token_hex(12)
                self.transient[identifier]={'owner':user.identity,'status':'in_progress','created':time.time(),'settings':asked.get('request',{}),'references':references,'kind':'video' if video else 'image'}
                async def execute():
                    """Run temporary image lines without retaining SQL job history."""
                    try:
                        identity='sha256:'+selected['content_sha256'] if selected.get('content_sha256') else selected.get('model_id')
                        async with self.batches.image_turn(identifier, [] if video else [(selected['id'],identity)],
                                                           lambda: self.users.by_identity(user.identity),
                                                           lambda: self.transient[identifier]['status']=='in_progress'):
                            for index,line in enumerate(lines):
                                current=self.users.by_identity(user.identity)
                                if not current:
                                    raise ValueError('The image job owner is no longer enabled.')
                                answer=await self.execute_line(identifier,{'line':index},line['body'],current,{'authorization':'Bearer '+self.batches.internal_key(current.name)},quiet=True)
                                if answer.status_code>=400:
                                    raise ValueError(answer.json().get('error',{}).get('message','Generation failed.'))
                            self.transient[identifier]['status']='completed'
                    except asyncio.CancelledError:
                        self.transient[identifier]['status']='cancelled'
                        raise
                    except Exception as problem:
                        self.transient[identifier].update(status='failed',error=str(problem))
                    finally:
                        self.transient[identifier]['completed_at']=time.time()
                task=asyncio.create_task(execute())
                self.transient[identifier]['task']=task
                self.batches.carrying.add(task)
                task.add_done_callback(self.batches.carrying.discard)
                return JSONResponse({'id':identifier,'temporary':True},201)
            self.batches.store.models=__import__('turnstile.batches',fromlist=['Pins']).Pins(await self.batches.model_ids(user))
            source=self.batches.store.put_file(user.name,'images.jsonl','batch',b'')
            self.store.bind_upload(source['id'],user.identity)
            batch=self.batches.store.start(user.name,'/v1/videos/generations' if video else IMAGE_PATHS[0],source['id'],lines,{'mirrorwall':True,'owner_id':user.identity})
            self.store.bind_job(batch['id'],user.identity,selected.get('model_id',''),selected['id'],asked,references)
            self.batches.work(batch['id'],self.users.by_name)
            return JSONResponse({'id':batch['id'],'temporary':False},201)
        except (ValueError,KeyError,TypeError) as problem:
            return error(400,str(problem))

    async def job_lines(self,request,user):
        """Every line of one of the caller's image jobs, in order, with the settings it varied, its outcome and its image."""
        number=request.path_params['identifier']
        if self.store.owner(number)!=user.identity:
            return error(404,'There is no such image job of yours.')
        images={}
        for identifier,metadata,width,height in self.store.db.execute(
                "SELECT a.id,a.metadata,a.width,a.height FROM mirrorwall_assets a JOIN mirrorwall_job_assets j ON j.asset=a.id "
                "WHERE j.job=? AND j.role='output' AND a.owner_id=?",(number,user.identity)):
            metadata=json.loads(metadata)
            images.setdefault(str(metadata.get('batch_line')),{'asset_id':identifier,'width':width,'height':height,'seed':metadata.get('seed')})
        lines=[]
        for line,body,result in self.store.db.execute('SELECT line,body,result FROM batch_lines WHERE batch=? ORDER BY line',(number,)):
            body=json.loads(body)
            outcome=json.loads(result) if result else None
            failed=bool(outcome and outcome.get('status_code',200)>=400)
            found=images.get(str(line))
            lines.append({'line':line,**{key:body.get(key) for key in ('sampler','scheduler','seed','steps','cfg_scale','guidance','width','height')},
                          'state':'failed' if failed else 'done' if found else 'waiting',
                          'error':((outcome.get('body') or {}).get('error') or {}).get('message','Generation failed.') if failed else None,
                          **(found or {})})
        batch=self.batches.store.get(number)
        return JSONResponse({'id':number,'status':batch['status'] if batch else 'completed','lines':lines,
                             'progress':{'stage':'queued'} if number in self.batches.image_waiting else self.progress.get(number)},
                            headers={'cache-control':'no-store'})

    async def remove_job(self,request,user):
        """Delete one of the caller's finished image jobs and the images it made."""
        number=request.path_params['identifier']
        if number in self.transient:
            return error(409,'This job is still running; stop it before deleting it.')
        try:
            if not self.store.delete_job(user.identity,number):
                return error(404,'There is no such image job of yours.')
        except ValueError as problem:
            return error(409,str(problem))
        return JSONResponse({'deleted':True,'id':number})

    async def clear_comparisons(self,request,user):
        """Delete every finished sampler comparison of the caller's; running ones are kept."""
        deleted,kept=[],0
        for number in self.store.comparison_jobs(user.identity):
            try:
                self.store.delete_job(user.identity,number) and deleted.append(number)
            except ValueError:
                kept+=1
        return JSONResponse({'deleted':deleted,'running':kept})

    async def control(self,request,user):
        """Stop/pause/resume only the caller's existing image batch."""
        number=request.path_params['identifier']
        if self.job_owner(number)!=user.identity:
            return error(404,'There is no such image job of yours.')
        asked=await self.read_json(request)
        action=asked.get('action','cancel')
        if number in self.transient:
            observation.silence()
            if action!='cancel':
                return error(400,'Temporary jobs support Stop; pause/resume requires retained work.')
            self.transient[number]['task'].cancel()
            return JSONResponse({'id':number,'status':'cancelling'})
        batch=self.batches.store.get(number)
        if not batch:
            return error(404,'There is no such image job of yours.')
        if action=='cancel':
            if batch['status'] not in ('in_progress','paused','cancelling'):
                return error(400,'This job has already ended.')
            self.batches.store.cancelling(number)
            if (task:=self.batches.working.get(number)) and not task.done():
                task.cancel()
            else:
                self.batches.store.finish(number)
        elif action=='pause':
            with self.batches.store.db:
                self.batches.store.db.execute("UPDATE batches SET status='paused' WHERE id=? AND status='in_progress'",(number,))
        elif action=='resume' and batch['status']=='paused':
            with self.batches.store.db:
                self.batches.store.db.execute("UPDATE batches SET status='in_progress' WHERE id=?",(number,))
            if not (task:=self.batches.working.get(number)) or task.done():
                self.batches.work(number,self.users.by_name)
        else:
            return error(400,'This job cannot perform that action.')
        return JSONResponse({'id':number,'status':self.batches.store.get(number)['status']})

    async def preferences(self,request,user):
        """Keep individual opt-ins private and subordinate to the global policy."""
        try:
            allowed=bool(self.adult_previews_allowed())
            asked=await self.read_json(request) if request.method=='POST' else None
            if asked and asked.get('adult_previews') is True and not allowed:
                return error(403,'Adult previews are disabled in system settings.')
            value=self.store.preferences(user.identity,asked)
            return JSONResponse({**value,'adult_previews':allowed and value.get('adult_previews',False),
                                 'adult_previews_allowed':allowed},headers={'cache-control':'no-store'})
        except ValueError as problem:
            return error(400,str(problem))

    async def preset(self,request,user):
        """Validate and save an owned workflow version."""
        try:
            asked=await self.read_json(request)
            settings=asked.get('settings',{})
            if not isinstance(settings,dict) or settings.keys()&{'model','prompt','image','references','mask','metadata','user'}:
                raise ValueError('Workflow settings cannot contain model overrides, prompts or private image inputs.')
            video=request.query_params.get('kind')=='video'
            from mirrorwall.video import checked_request as check_video
            check=check_video if video else checked_request
            check({'model':asked.get('model'),'prompt':'Validate preset',**settings})
            selected=next((row for row in await self.media_catalogue(user,'video' if video else 'image') if row['id']==asked.get('model')),None)
            if not selected:
                raise ValueError('Choose an available image model.')
            profile=selected.get('video_workflow' if video else 'image_workflow',{})
            if profile:
                check({'model':selected['id'],'prompt':'Validate preset',**settings},
                    {'sdcpp':{**profile,**({'mode':'vid_gen'} if video else {'upscaler':profile.get('upscaling')})},'alone':profile.get('exclusive',True)})
            owner='*' if asked.get('published') and user.can('manage-models') else user.identity
            if asked.get('published') and owner!='*':
                return error(403,'Publishing presets requires model-management permission.')
            return JSONResponse(self.store.save_preset(owner,asked.get('name',''),selected['id'],selected.get('model_id',''),settings,asked.get('id'),asked.get('version')))
        except (ValueError,TypeError) as problem:
            return error(400,str(problem))

    async def workflows(self,request,user):
        """Read the permitted workflow library without fetching images or job history."""
        try:
            models=await self.media_catalogue(user,'video' if request.query_params.get('kind')=='video' else 'image')
        except ValueError as problem:
            return error(503,str(problem))
        return JSONResponse({'models':models,'presets':self.visible_presets(user,models),'can_manage':user.can('manage-models')},headers={'cache-control':'no-store'})

    async def remove_preset(self, request, user):
        """Remove a private workflow as its owner, or a shared workflow as a model manager."""
        identifier = request.path_params['identifier']
        row = next((row for row in self.store.presets(user.identity) if row['id'] == identifier), None)
        if not row:
            return error(404, 'There is no such workflow.')
        if row['published'] and not user.can('manage-models'):
            return error(403, 'Removing a shared workflow requires model-management permission.')
        try:
            version = int(request.query_params.get('version', ''))
        except ValueError:
            return error(400, 'Choose the saved workflow version to remove.')
        if not self.store.remove_preset('*' if row['published'] else user.identity, identifier, version):
            return error(409, 'This workflow changed elsewhere. Reload and review it before removing it.')
        return JSONResponse({'removed': identifier}, headers={'cache-control': 'no-store'})

    async def preset_history(self,request,user):
        """Keep workflow revisions behind current ownership and shared-model access."""
        identifier=request.path_params['identifier']
        row=next((row for row in self.store.presets(user.identity) if row['id']==identifier),None)
        if not row:
            return error(404,'There is no such workflow.')
        if row['published']:
            try:
                allowed=await self.media_catalogue(user,'video' if request.query_params.get('kind')=='video' else 'image')
            except ValueError as problem:
                return error(503,str(problem))
            if not any(m.get('model_id')==row['model_id'] if row['model_id'] else m['id']==row['model'] for m in allowed):
                return error(404,'There is no such workflow.')
        return JSONResponse({'versions':self.store.preset_history(user.identity,identifier)},headers={'cache-control':'no-store'})

    async def graph_models(self,user):
        """The caller's image models with their families, for workflow checks and access."""
        return [{'id':m['id'],'family':(m.get('image_workflow') or {}).get('family'),'control':bool((m.get('image_workflow') or {}).get('control'))}
                for m in await self.media_catalogue(user,'image')]

    async def graph_upscalers(self,user):
        """Installed upscaler files for a workflow manager, or None when the inventory cannot be read (checks are then skipped)."""
        try:
            answer=await self.batches.door.get('/turnstile/models/upscalers',headers={'authorization':'Bearer '+self.batches.internal_key(user.name),'x-turnstile-no-wake':'1'})
        except Exception:
            return None
        return answer.json().get('upscalers',[]) if answer.status_code==200 else None

    @staticmethod
    def upscaler_names(upscalers):
        """The installed upscalers' file IDs, or None when the inventory could not be read."""
        return None if upscalers is None else [one['id'] for one in upscalers]

    def runnable(self,row,models):
        """A published workflow's caller view, when the caller may use every model it uses."""
        found=self.store.runnable_graph(row['api_name']) if row['published'] else None
        if not found or not set(graph.models_of(found['document']))<={m['id'] for m in models}:
            return None
        return {'id':row['id'],'version':found['version'],**graph.interface(found['document'])}

    async def graph_library(self,request,user):
        """Workflows the caller may run; managers also see drafts, run problems, sample templates and the catalogue."""
        try:
            models=await self.graph_models(user)
        except ValueError as problem:
            return error(503,str(problem))
        manage=user.can('manage-workflows')
        if manage:
            upscalers=await self.graph_upscalers(user)
            names=self.upscaler_names(upscalers)
            body={'workflows':[{**row,'problems':graph.problems(row['document'],models,names)} for row in self.store.graphs()],
                  'templates':graph.SAMPLES,'catalogue':graph.catalogue(),'upscalers':upscalers}
        else:
            body={'workflows':[one for row in self.store.graphs() if (one:=self.runnable(row,models))]}
        return JSONResponse({**body,'models':models,'can_manage':manage},headers={'cache-control':'no-store'})

    async def graph_detail(self,request,user):
        """A manager reads every version; a caller reads only a runnable workflow's interface."""
        row=self.store.graph(request.path_params['identifier'])
        try:
            models=await self.graph_models(user) if row else []
        except ValueError as problem:
            return error(503,str(problem))
        if row and user.can('manage-workflows'):
            names=self.upscaler_names(await self.graph_upscalers(user))
            return JSONResponse({**row,'problems':graph.problems(row['document'],models,names),'versions':self.store.graph_versions(row['id'])},
                                headers={'cache-control':'no-store'})
        found=self.runnable(row,models) if row else None
        return JSONResponse(found,headers={'cache-control':'no-store'}) if found else error(404,'There is no such workflow.')

    async def graph_save(self,request,user):
        """Check and save a workflow document as a new version."""
        if not user.can('manage-workflows'):
            return error(403,'Saving image workflows requires the manage-workflows ability.')
        try:
            asked=await self.read_json(request)
            if not isinstance(asked,dict) or asked.keys()-{'id','version','document'}:
                raise ValueError('Send id, version and document.')
            document,problems=graph.check(asked.get('document'),await self.graph_models(user),self.upscaler_names(await self.graph_upscalers(user)))
            row=self.store.save_graph(user.identity,document,asked.get('id'),asked.get('version'))
            return JSONResponse({**row,'problems':problems},201 if row['version']==1 else 200,headers={'cache-control':'no-store'})
        except (ValueError,TypeError) as problem:
            return error(400,str(problem))

    async def graph_publish(self,request,user):
        """Publish a version with no run problems, or unpublish with version 0."""
        if not user.can('manage-workflows'):
            return error(403,'Publishing image workflows requires the manage-workflows ability.')
        try:
            asked=await self.read_json(request)
            identifier,version,expected=request.path_params['identifier'],asked.get('version'),asked.get('expected_version')
            if type(version) is not int or type(expected) is not int or version<0:
                raise ValueError('Send the version to publish (0 unpublishes) and the expected latest version.')
            if not self.store.graph(identifier):
                return error(404,'There is no such workflow.')
            if version:
                chosen=next((one for one in self.store.graph_versions(identifier) if one['version']==version),None)
                if not chosen:
                    raise ValueError('There is no such version of this workflow.')
                failing=[one['text'] for one in graph.problems(chosen['document'],await self.graph_models(user),
                                                               self.upscaler_names(await self.graph_upscalers(user))) if one['level']=='error']
                if failing:
                    raise ValueError('Fix these problems before publishing: '+' '.join(failing))
            return JSONResponse(self.store.publish_graph(identifier,version,expected),headers={'cache-control':'no-store'})
        except (ValueError,TypeError) as problem:
            return error(400,str(problem))

    async def graph_remove(self,request,user):
        """Delete a workflow at its reviewed latest version; saved images and job records remain."""
        if not user.can('manage-workflows'):
            return error(403,'Removing image workflows requires the manage-workflows ability.')
        identifier=request.path_params['identifier']
        if not self.store.graph(identifier):
            return error(404,'There is no such workflow.')
        try:
            version=int(request.query_params.get('version',''))
        except ValueError:
            return error(400,'Choose the saved workflow version to remove.')
        if not self.store.remove_graph(identifier,version):
            return error(409,'This workflow changed elsewhere. Reload and review it before removing it.')
        return JSONResponse({'removed':identifier},headers={'cache-control':'no-store'})

    def stage_sender(self,user,request_headers):
        """Send one workflow step through the caller's own image API access; its image is not saved."""
        passed={key:value for key,value in request_headers.items() if key in ('x-loadcoach-log','x-loadcoach-trace','x-loadcoach-sensitivity','x-loadcoach-priority')}
        async def send(path,body):
            """Stream one intermediate image request back through turnstile as the run's owner, tagging finished images with their node."""
            headers={**passed,'authorization':'Bearer '+self.batches.internal_key(user.name),'x-mirrorwall-intermediate':'1'}
            async with self.batches.door.stream('POST',path,json=body,headers=headers,timeout=None) as response:
                if response.status_code>=400:
                    await response.aread()
                    try:
                        detail=response.json().get('error',{})
                    except ValueError:
                        detail={}
                    yield {'type':'error','error':{'message':(detail.get('message') if isinstance(detail,dict) else str(detail)) or f'HTTP {response.status_code}'}}
                    return
                node=response.headers.get('x-loadcoach-node')
                async for event in stream.events(response.aiter_bytes()):
                    yield {**event,'node':node} if event.get('type') in ('image_generation.completed','image_edit.completed') else event
        return send

    async def runnable_workflows(self,user):
        """Published workflows the caller may run: every model each one uses must be one of theirs."""
        models=await self.graph_models(user)
        return [one for row in self.store.graphs() if (one:=self.runnable(row,models))],models

    async def workflow_listing(self,user):
        """`GET /v1/workflows`: what a caller may run, with inputs, overridable fields, outputs and models."""
        try:
            found,_=await self.runnable_workflows(user)
        except ValueError as problem:
            return error(503,str(problem))
        return JSONResponse({'object':'list','data':[{**one,'id':'workflow:'+one['api_name'],'object':'workflow','workflow_id':one['id']} for one in found]},
                            headers={'cache-control':'no-store'})

    async def execute_workflow(self,user,document,asked,*,headers,quiet,identity,image_of=None):
        """Yield a prepared run's events; Output images are saved to the caller's library with the workflow and step settings."""
        models=await self.graph_models(user)
        failing=[one['text'] for one in graph.problems(document,models) if one['level']=='error']
        if failing:
            raise graph_run.RunError('This workflow cannot run: '+' '.join(failing))
        prepared,count,images=graph_run.prepare(document,asked)
        upscalers=await self.graph_upscalers(user) if user.can('manage-models') or user.can('manage-workflows') else None
        async for event in graph_run.run(prepared,count,images,models=models,upscalers=upscalers,
                                         send=self.stage_sender(user,headers),image_of=image_of):
            if event['type']=='workflow.output':
                saved=self.retain_event(user,{**event,'type':'image_generation.completed','workflow':identity},quiet=quiet)
                event={**event,'asset_id':saved['asset_id'],'asset_url':saved['asset_url']}
            yield event

    async def workflow_api(self,request,user,asked):
        """`model="workflow:<name>[@version]"` on the image endpoints: run a published workflow as the caller."""
        try:
            name,version=graph_run.parse_model(asked.get('model'))
            models=await self.graph_models(user)
            found=self.store.runnable_graph(name,version)
            if not found or not set(graph.models_of(found['document']))<={m['id'] for m in models}:
                return error(404,'There is no such workflow.')
            quiet=request.headers.get('x-loadcoach-log')=='off'
            if quiet and not await self.batches.can_unlogged(user):
                return error(403,'Your account or the current compute policy does not permit no-log generation.')
            identity={'id':found['id'],'api_name':found['api_name'],'version':found['version']}
            graph_run.prepare(found['document'],asked)  # refuse bad inputs before anything starts
        except graph_run.RunError as problem:
            return error(400,str(problem))
        except ValueError as problem:
            return error(503,str(problem))
        sequence=self.execute_workflow(user,found['document'],asked,headers=request.headers,quiet=quiet,identity=identity,image_of=None)
        kind='image_edit' if request.url.path.endswith('/edits') else 'image_generation'
        if asked.get('stream'):
            async def events():
                """Encode the workflow's events for the API stream, naming outputs by the request kind and ending with completion or an error."""
                try:
                    async for event in sequence:
                        if event['type']=='workflow.output':
                            event={**event,'type':kind+'.completed'}
                        yield stream.encode(event)
                    yield stream.encode({'type':'workflow.completed','workflow':identity})
                except graph_run.RunError as problem:
                    yield stream.encode({'type':'error','error':{'message':str(problem)}})
            return StreamingResponse(events(),media_type='text/event-stream',headers={'cache-control':'no-store'})
        data=[]
        try:
            async for event in sequence:
                if event['type']=='workflow.output':
                    data.append({key:value for key,value in event.items() if key!='type'})
        except graph_run.RunError as problem:
            return error(502 if data else 400,str(problem))
        return JSONResponse({'created':int(time.time()),'data':data,'workflow':identity},headers={'cache-control':'no-store'})

    async def graph_run(self,request,user):
        """Start a manager's test run of a saved version or the unsaved draft; the page polls its state. Outputs go to the library."""
        if not user.can('manage-workflows'):
            return error(403,'Test runs require the manage-workflows ability.')
        row=self.store.graph(request.path_params['identifier']) if request.path_params['identifier']!='draft' else None
        try:
            asked=await self.read_json(request)
            if not isinstance(asked,dict) or asked.keys()-{'document','version','inputs','n','temporary'}:
                raise ValueError('Send document or version, inputs, n and temporary.')
            models=await self.graph_models(user)
            if asked.get('document') is not None:
                document,_=graph.check(asked['document'],models)
                identity={'id':row['id'] if row else None,'api_name':document['api_name'],'version':None,'draft':True}
            else:
                if not row:
                    return error(404,'There is no such workflow.')
                chosen=next((one for one in self.store.graph_versions(row['id']) if one['version']==asked.get('version',row['version'])),None)
                if not chosen:
                    raise ValueError('There is no such version of this workflow.')
                document,identity=chosen['document'],{'id':row['id'],'api_name':row['api_name'],'version':chosen['version']}
            if not set(graph.models_of(document))<={m['id'] for m in models}:
                raise ValueError('You may not use every model this workflow uses.')
            temporary=asked.get('temporary',False)
            if type(temporary) is not bool:
                raise ValueError('temporary must be true or false.')
            if temporary and not await self.batches.can_unlogged(user):
                return error(403,'Your account or the current compute policy does not permit no-log generation.')
            run={'n':asked.get('n',1),'workflow_inputs':asked.get('inputs') or {}}
            graph_run.prepare(document,run)
        except (ValueError,TypeError) as problem:
            return error(400,str(problem))
        def image_of(identifier):
            """A saved image of this owner's library as base64, or None if it is not theirs or gone."""
            content=self.store.content(user.identity,identifier)
            return base64.b64encode(content).decode() if content else None
        for identifier,row in list(self.graph_runs.items()):
            if row['status']!='running' and time.time()-row['updated']>3600:
                self.graph_runs.pop(identifier)
        if sum(row['owner']==user.identity and row['status']=='running' for row in self.graph_runs.values())>=2:
            return error(429,'Two test runs are already running; wait for one or stop it.')
        identifier='graph_run_'+secrets.token_hex(12)
        state=self.graph_runs[identifier]={'id':identifier,'owner':user.identity,'status':'running','workflow':identity,'events':[],
                                           'outputs':[],'steps':{},'error':None,'created':time.time(),'updated':time.time()}
        headers={'x-loadcoach-log':'off'} if temporary else {}
        async def execute():
            """Run the workflow in the background, recording each step, output and the final state for polling."""
            try:
                async for event in self.execute_workflow(user,document,run,headers=headers,quiet=temporary,identity=identity,image_of=image_of):
                    event={key:value for key,value in event.items() if key!='b64_json'}  # the page loads saved images by URL
                    if event['type']=='workflow.output':
                        state['outputs'].append(event)
                    elif event['type']=='workflow.step':
                        state['steps'][f"{event['item']}:{event['step']}"]=event
                    elif event['type']=='workflow.started':
                        state['plan']=event
                    state['events']=[*state['events'][-39:],{key:value for key,value in event.items() if key!='stages'}]
                    state['updated']=time.time()
                state['status']='completed'
            except asyncio.CancelledError:
                state['status']='stopped'
                raise
            except graph_run.RunError as problem:
                state.update(status='failed',error=str(problem))
            except Exception as problem:  # a stage that broke the connection: shown, never hidden
                state.update(status='failed',error='The run stopped: '+(str(problem) or type(problem).__name__))
            finally:
                state['updated']=time.time()
        task=asyncio.create_task(execute())
        state['task']=task
        self.batches.carrying.add(task)
        task.add_done_callback(self.batches.carrying.discard)
        return JSONResponse(self.graph_run_view(state),201,headers={'cache-control':'no-store'})

    @staticmethod
    def graph_run_view(state):
        """A test run's state for its owner's page, without the background task or owner identity."""
        return {key:value for key,value in state.items() if key not in ('task','owner')}

    async def graph_run_state(self,request,user):
        """A test run's progress and outputs, for the manager who started it."""
        state=self.graph_runs.get(request.path_params['identifier'])
        if not state or state['owner']!=user.identity:
            return error(404,'There is no such test run of yours.')
        return JSONResponse(self.graph_run_view(state),headers={'cache-control':'no-store'})

    async def graph_run_stop(self,request,user):
        """Stop a running test run; the step in progress is abandoned and nothing after it starts."""
        state=self.graph_runs.get(request.path_params['identifier'])
        if not state or state['owner']!=user.identity:
            return error(404,'There is no such test run of yours.')
        if state['status']=='running':
            state['task'].cancel()
            state['status']='stopped'
            state['updated']=time.time()
        return JSONResponse(self.graph_run_view(state),headers={'cache-control':'no-store'})

    def routes(self,caller,log):
        """Expose control-node-owned image operations through the existing API-key authority."""
        from turnstile.accounts import signed_in
        def route(path,handler,method='GET'):
            """Register an image handler behind existing API authentication."""
            return Route(path,signed_in(caller,log,handler),methods=[method])
        return [route('/mirrorwall/state',self.state),route('/mirrorwall/assets',self.assets),route('/mirrorwall/assets',self.upload,'POST'),
                route('/mirrorwall/assets/{identifier}',self.asset,'DELETE'),
                route('/mirrorwall/assets/{identifier}/content',self.asset),route('/mirrorwall/assets/{identifier}/thumbnail',self.asset),
                route('/mirrorwall/jobs',self.start,'POST'),route('/mirrorwall/jobs/{identifier}',self.control,'POST'),
                route('/mirrorwall/jobs/{identifier}',self.remove_job,'DELETE'),route('/mirrorwall/comparisons',self.clear_comparisons,'DELETE'),
                route('/mirrorwall/jobs/{identifier}/lines',self.job_lines),
                route('/mirrorwall/preferences',self.preferences),route('/mirrorwall/preferences',self.preferences,'POST'),route('/mirrorwall/presets',self.preset,'POST'),
                route('/mirrorwall/workflows',self.workflows),route('/mirrorwall/presets/{identifier}',self.preset_history),
                route('/mirrorwall/presets/{identifier}',self.remove_preset,'DELETE'),
                route('/mirrorwall/graphs',self.graph_library),route('/mirrorwall/graphs',self.graph_save,'POST'),
                route('/mirrorwall/graphs/{identifier}',self.graph_detail),route('/mirrorwall/graphs/{identifier}',self.graph_remove,'DELETE'),
                route('/mirrorwall/graphs/{identifier}/publish',self.graph_publish,'POST'),
                route('/mirrorwall/graphs/{identifier}/run',self.graph_run,'POST'),
                route('/mirrorwall/graph-runs/{identifier}',self.graph_run_state),
                route('/mirrorwall/graph-runs/{identifier}/stop',self.graph_run_stop,'POST')]
