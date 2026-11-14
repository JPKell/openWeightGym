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
from mirrorwall import stream

IMAGE_PATHS=("/v1/images/generations","/v1/images/edits")


def error(status,message):
    """Return a consistent compatible image error without exposing other owners."""
    return JSONResponse({"error":{"message":message}},status)


class Front:
    """Mirror Wall's control-node-owned assets/presets and ordinary batch execution hooks."""
    def __init__(self,store,batches,users,*,adult_previews_allowed=lambda: False):
        """Initialize private image state without loading model weights."""
        self.store=store
        self.batches=batches
        self.users=users
        self.adult_previews_allowed=adult_previews_allowed
        self.progress={}
        self.transient={}

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

    async def catalogue(self,user):
        """Use the authenticated combined catalogue; never reveal forbidden models."""
        answer=await self.batches.door.get('/v1/models',headers={'authorization':'Bearer '+self.batches.internal_key(user.name), 'x-turnstile-no-wake':'1'})
        if answer.status_code!=200:
            raise ValueError("Image model inventory is unavailable.")
        return [one for one in answer.json().get('data',[]) if one.get('capabilities',{}).get('image_generation')]

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
        if job and kind in ('image_generation.partial_image','image_generation.progress','image_edit.partial_image','image_edit.progress'):
            self.progress[job]={**event,'at':time.time()}
        if job and 'error' in event:
            self.progress[job]={**event,'stage':'failed','at':time.time()}
        if kind not in ('image_generation.completed','image_edit.completed'):
            return event
        content=base64.b64decode(event['b64_json'],validate=True)
        metadata={k:v for k,v in event.items() if k not in ('b64_json','type')}
        if job:
            metadata.update(job=job,batch_line=line)
        asset=self.store.save(user.identity,content,kind='output',filename='generated-'+str(event.get('seed',0))+'.png',
                              metadata=metadata,temporary=quiet,job=job if job and not quiet else None)
        if job:
            self.progress[job]={'type':'image_generation.progress','stage':'completed','at':time.time()}
        return {**event,'asset_id':asset['id'],'asset_url':'/mirrorwall/assets/'+asset['id']+'/content'}

    async def capture(self,request,user,response):
        """Retain allowed completed image responses on the control node while forwarding real previews."""
        if response.status_code>=400:
            return response
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
                    async for event in stream.events(chunks()):
                        yield stream.encode(self.retain_event(user,{**event,**provenance} if event.get('type') in ('image_generation.completed','image_edit.completed') else event,job,line,quiet))
                except Exception as problem:
                    yield stream.encode({'type':'error','error':{'message':str(problem)}})
                finally:
                    iterator=getattr(response,'body_iterator',None)
                    if iterator and hasattr(iterator,'aclose'):
                        await iterator.aclose()
            return StreamingResponse(forwarded(),media_type='text/event-stream',headers=headers,background=response.background)
        body=bytearray()
        async for chunk in chunks():
            body.extend(chunk)
            if len(body)>256*2**20:
                return error(502,"Image response exceeds the configured response limit.")
        try:
            decoded=json.loads(body)
            for index,item in enumerate(decoded.get('data',[])):
                event=self.retain_event(user,{'type':'image_generation.completed','image_index':index,**item,**provenance},job,line,quiet)
                item.update({k:v for k,v in event.items() if k not in ('type','image_index')})
            return JSONResponse(decoded,headers=headers,background=response.background)
        except (ValueError,KeyError,TypeError) as problem:
            return error(502,'Could not retain the generated image: '+str(problem))

    async def execute_line(self,number,line,body,user,headers,*,quiet=False):
        """Execute an existing batch line through the same authenticated image API."""
        if self.job_owner(number)!=user.identity:
            return httpx.Response(403,json={'error':{'message':'Image job owner changed.'}})
        previous=[asset for asset in self.store.listing(user.identity,kind='output',job=number) if str(asset['metadata'].get('batch_line'))==str(line['line'])]
        if previous:
            return httpx.Response(200,json={'data':[{'asset_id':asset['id'],**asset['metadata']} for asset in previous]})
        sent=dict(body)
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
        async with self.batches.door.stream('POST','/v1/images/edits' if references or mask else '/v1/images/generations',json=sent,headers=headers,timeout=None) as response:
            request_id=response.headers.get('x-loadcoach-request-id','')
            if response.status_code>=400:
                await response.aread()
                return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
            async for event in stream.events(response.aiter_bytes()):
                if 'error' in event:
                    return httpx.Response(502,json={'error':event['error']})
                if event.get('type') in ('image_generation.completed','image_edit.completed'):
                    result.append({k:v for k,v in event.items() if k not in ('type','b64_json')})
        if not result:
            return httpx.Response(502,json={'error':{'message':'The image stream ended without a completed image.'}})
        return httpx.Response(200,json={'created':int(time.time()),'data':result},headers={'x-loadcoach-request-id':request_id})

    async def state(self,request,user):
        """List permitted models, private assets/jobs and versioned workflow presets."""
        for identifier,row in list(self.transient.items()):
            if row['status']!='in_progress' and time.time()-row['created']>3600:
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
                         'request_counts':batch.get('request_counts',{}) if batch else {},'progress':self.progress.get(row['id']),'error':failure})
        for identifier,row in self.transient.items():
            if row['owner']==user.identity:
                jobs.insert(0,{'id':identifier,**{k:v for k,v in row.items() if k not in ('owner','task')},'progress':self.progress.get(identifier)})
        return JSONResponse({'models':models,'assets':self.store.listing(user.identity,kind='output'),
             'references':self.store.listing(user.identity,kind='reference'),'presets':self.visible_presets(user,models),
             'jobs':jobs,'preferences':self.store.preferences(user.identity),'can_manage':user.can('manage-models'),'can_unlogged':user.can('unlogged')},headers={'cache-control':'no-store'})

    async def assets(self,request,user):
        """Page through the caller's retained image library without loading pixels."""
        try:
            kind=request.query_params.get('kind','output')
            if kind not in ('output','reference'):
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
            asked=await self.read_json(request)
            temporary=asked.get('temporary',False)
            if type(temporary) is not bool:
                raise ValueError('temporary must be true or false.')
            if temporary and not user.can('unlogged'):
                return error(403,'Your account may not use temporary no-log images.')
            if temporary:
                observation.silence()
            content=base64.b64decode(asked.get('b64_json',''),validate=True)
            asset=self.store.save(user.identity,content,kind=asked.get('kind','reference'),filename=asked.get('filename','reference.png'),temporary=temporary)
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
            suffix='.png' if content.startswith(b'\x89PNG') else '.jpg' if content.startswith(b'\xff\xd8') else '.webp'
            headers['content-disposition']='attachment; filename="'+identifier+suffix+'"'
        return Response(content,media_type='image/png' if request.url.path.endswith('/thumbnail') or content.startswith(b'\x89PNG') else 'image/jpeg' if content.startswith(b'\xff\xd8') else 'image/webp',headers=headers)

    @staticmethod
    async def read_json(request):
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
        if not isinstance(sweep,dict) or sweep.keys()-{'seed','steps','guidance','cfg_scale','width','height'}:
            raise ValueError('Unsupported sweep field.')
        if any(not isinstance(values,list) or not values or len(values)>32 for values in sweep.values()):
            raise ValueError('Each sweep requires 1–32 values.')
        checked_request(body)
        count=body.pop('n',1)
        total=count*math.prod(len(values) for values in sweep.values())
        if total>128:
            raise ValueError('A generation job is limited to 128 images.')
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
            lines=self.lines(asked)
            models=await self.catalogue(user)
            selected=next((one for one in models if one['id']==lines[0]['body']['model']),None)
            if not selected:
                raise ValueError('Choose an available image model you may use.')
            references=[*asked.get('references',[]),*([asked['mask']] if asked.get('mask') else [])]
            for identifier in references:
                if not self.store.asset(user.identity,identifier):
                    raise ValueError('There is no such reference image of yours.')
            self.fits(selected,asked,user)
            temporary=asked.get('temporary',False)
            if type(temporary) is not bool:
                raise ValueError('temporary must be true or false.')
            if any(self.store.asset(user.identity,key).get('temporary') for key in references) and not temporary:
                raise ValueError('Temporary references require a temporary no-log job.')
            if temporary:
                if not user.can('unlogged'):
                    return error(403,'Your account may not make no-log requests.')
                observation.silence()
                identifier='quiet_image_'+secrets.token_hex(12)
                self.transient[identifier]={'owner':user.identity,'status':'in_progress','created':time.time(),'settings':asked.get('request',{}),'references':references}
                async def execute():
                    """Run temporary image lines without retaining SQL job history."""
                    try:
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
                task=asyncio.create_task(execute())
                self.transient[identifier]['task']=task
                self.batches.carrying.add(task)
                task.add_done_callback(self.batches.carrying.discard)
                return JSONResponse({'id':identifier,'temporary':True},201)
            self.batches.store.models=__import__('turnstile.batches',fromlist=['Pins']).Pins(await self.batches.model_ids(user))
            source=self.batches.store.put_file(user.name,'images.jsonl','batch',b'')
            self.store.bind_upload(source['id'],user.identity)
            batch=self.batches.store.start(user.name,IMAGE_PATHS[0],source['id'],lines,{'mirrorwall':True,'owner_id':user.identity})
            self.store.bind_job(batch['id'],user.identity,selected.get('model_id',''),selected['id'],asked,references)
            self.batches.work(batch['id'],self.users.by_name)
            return JSONResponse({'id':batch['id'],'temporary':False},201)
        except (ValueError,KeyError,TypeError) as problem:
            return error(400,str(problem))

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
            checked_request({'model':asked.get('model'),'prompt':'Validate preset',**settings})
            selected=next((row for row in await self.catalogue(user) if row['id']==asked.get('model')),None)
            if not selected:
                raise ValueError('Choose an available image model.')
            profile=selected.get('image_workflow',{})
            if profile:
                checked_request({'model':selected['id'],'prompt':'Validate preset',**settings},
                    {'sdcpp':{**profile,'upscaler':profile.get('upscaling')},'alone':profile.get('exclusive',True)})
            owner='*' if asked.get('published') and user.can('manage-models') else user.identity
            if asked.get('published') and owner!='*':
                return error(403,'Publishing presets requires model-management permission.')
            return JSONResponse(self.store.save_preset(owner,asked.get('name',''),selected['id'],selected.get('model_id',''),settings,asked.get('id'),asked.get('version')))
        except (ValueError,TypeError) as problem:
            return error(400,str(problem))

    async def workflows(self,request,user):
        """Read the permitted workflow library without fetching images or job history."""
        try:
            models=await self.catalogue(user)
        except ValueError as problem:
            return error(503,str(problem))
        return JSONResponse({'models':models,'presets':self.visible_presets(user,models),'can_manage':user.can('manage-models')},headers={'cache-control':'no-store'})

    async def preset_history(self,request,user):
        """Keep workflow revisions behind current ownership and shared-model access."""
        identifier=request.path_params['identifier']
        row=next((row for row in self.store.presets(user.identity) if row['id']==identifier),None)
        if not row:
            return error(404,'There is no such workflow.')
        if row['published']:
            try:
                allowed=await self.catalogue(user)
            except ValueError as problem:
                return error(503,str(problem))
            if not any(m.get('model_id')==row['model_id'] if row['model_id'] else m['id']==row['model'] for m in allowed):
                return error(404,'There is no such workflow.')
        return JSONResponse({'versions':self.store.preset_history(user.identity,identifier)},headers={'cache-control':'no-store'})

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
                route('/mirrorwall/preferences',self.preferences),route('/mirrorwall/preferences',self.preferences,'POST'),route('/mirrorwall/presets',self.preset,'POST'),
                route('/mirrorwall/workflows',self.workflows),route('/mirrorwall/presets/{identifier}',self.preset_history)]
