"""Local Videos API backed by Mirror Wall assets and the existing control-node batch executor.

The resource shape follows the historical OpenAI Videos API. Local WebM, fractional seconds,
asset IDs, no-log requests and cancellation are explicit Suite extensions.
"""
import base64
import json
import re
import time

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach import observability as observation
from mirrorwall.front import error
from mirrorwall.video import checked_request


async def prepare(front,request,user,body):
    """Resolve only owned image assets or bounded inline images before binding a video job."""
    asked=dict(body)
    temporary=asked.pop('temporary',False) or request.headers.get('x-loadcoach-log')=='off'
    if type(temporary) is not bool:
        raise ValueError('temporary must be true or false.')
    if temporary:
        if not user.can('unlogged'):
            raise ValueError('Your account may not make no-log requests.')
        observation.silence()
    models=await front.catalogue(user,'video')
    selected=next((one for one in models if one['id']==asked.get('model')),None)
    if not selected:
        raise ValueError('Choose an available video model you may use.')
    profile=selected.get('video_workflow') or {}
    if not profile:
        raise ValueError('The selected model has no video profile evidence.')
    settings={'alone':True,'sdcpp':{**profile,'mode':'vid_gen'}}
    assets={}
    decoded=dict(asked)
    if 'input_reference' in decoded:
        if 'init_image' in decoded:
            raise ValueError('Use input_reference or init_image, not both.')
        decoded['init_image']=decoded.pop('input_reference')
    pending=[]
    for key in ('init_image','end_image','reference_images','control_video'):
        if key not in decoded:
            continue
        values=decoded[key] if key in ('reference_images','control_video') else [decoded[key]]
        if not isinstance(values,list) or len(values)>601:
            raise ValueError('Video image inputs must be a bounded list.')
        pixels=[]
        assets[key]=[]
        for value in values:
            # The historical input_reference object has an image_url; remote URLs are refused.
            if isinstance(value,dict):
                if value.keys() not in ({'asset_id'},{'image_url'}):
                    raise ValueError('Use an owned asset_id or inline image_url; remote files are unsupported.')
                value=value.get('asset_id',value.get('image_url'))
            record=front.store.asset(user.identity,value) if isinstance(value,str) and value.startswith('image_') else None
            if isinstance(value,str) and value.startswith('image_'):
                if not record or record['kind'] not in ('reference','output'):
                    raise ValueError('There is no such input image of yours.')
                if record['temporary'] and not temporary:
                    raise ValueError('Temporary inputs require a temporary no-log video.')
                content=front.store.content(user.identity,value)
                if content is None:
                    raise ValueError('An input image is unavailable.')
                pixels.append(base64.b64encode(content).decode())
                assets[key].append(value)
            else:
                from mirrorwall.requests import encoded_image
                pixels.append(encoded_image(value))
                pending.append((key,len(pixels)-1,pixels[-1]))
                assets[key].append(None)
        decoded[key]=pixels if key in ('reference_images','control_video') else pixels[0] if pixels else None
    resolved=checked_request(decoded,settings,account_limits=front.limits.for_user(user))
    made=[]
    try:
        for key,index,pixels in pending:
            record=front.store.save(user.identity,base64.b64decode(pixels,validate=True),kind='reference',
                                    temporary=temporary,limits=front.limits.for_user(user))
            made.append(record['id'])
            assets[key][index]=record['id']
    except BaseException:
        for identifier in made:
            front.store.delete(user.identity,identifier)
        raise
    payload={key:value for key,value in resolved.items() if key not in assets}
    payload['video_asset_ids']=assets
    envelope={'kind':'video','request':{key:value for key,value in payload.items() if key!='video_asset_ids'},
              'references':[identifier for values in assets.values() for identifier in values],'temporary':temporary}
    return envelope,[{'custom_id':'video-1','body':payload}]


class Videos:
    """Owner-scoped video job resources; current account identity is checked at every operation."""
    def __init__(self,front):
        """Reuse the existing store, account authority, progress and batch lifecycle."""
        self.front=front

    def resource(self,user,identifier):
        """Present existing batch state without exposing other owners or unrelated media jobs."""
        front=self.front
        if front.job_owner(identifier)!=user.identity:
            return None
        temporary=front.transient.get(identifier)
        row=temporary or front.store.job(user.identity,identifier)
        if not row or (row.get('kind')!='video' if temporary else row['settings'].get('kind')!='video'):
            return None
        settings=row.get('settings',{}) if temporary else row['settings'].get('request',{})
        batch=None if temporary else front.batches.store.get(identifier)
        status=temporary['status'] if temporary else batch['status'] if batch else 'failed'
        clips=front.store.listing(user.identity,kind='video',job=identifier)
        failure=temporary.get('error') if temporary else None
        if status=='completed' and not clips:
            status='expired';failure='The completed clip is no longer retained.'
        if batch and batch['request_counts'].get('failed'):
            status='failed'
            result=front.store.db.execute('SELECT result FROM batch_lines WHERE batch=? AND result IS NOT NULL ORDER BY line',(identifier,)).fetchone()
            failure=json.loads(result[0]).get('body',{}).get('error',{}).get('message') if result else 'Video generation failed.'
        progress=front.progress.get(identifier) or {}
        counts=progress.get('progress') if isinstance(progress.get('progress'),dict) else {}
        total=counts.get('total_steps',0)
        percent=min(95,round(100*counts.get('step',0)/total)) if type(total) in (int,float) and total>0 else 0
        return {'id':identifier,'object':'video','model':settings.get('model'),'prompt':settings.get('prompt'),
                'created_at':int(row['created']),'completed_at':int(clips[0]['created']) if clips else None,
                'status':'completed' if clips else 'failed' if status in ('failed','cancelled','expired') else 'queued' if not progress else 'in_progress',
                'error':{'code':status,'message':failure or 'Video generation '+status} if status in ('failed','cancelled','expired') else None,
                'seconds':str(settings.get('seconds',5)),'size':str(settings.get('width',832))+'x'+str(settings.get('height',480)),
                'progress':100 if clips else percent,'stage':progress.get('stage','queued'),'step':counts.get('step'),'total_steps':total,
                'queue_position':progress.get('queue_position'),
                'preview_b64':progress.get('preview_b64'),
                'frames':settings.get('frames'),'fps':settings.get('fps'),'temporary':bool(temporary),
                'asset_id':clips[0]['id'] if clips else None,'settings':settings,'assets':clips}

    async def create(self,request,user):
        """POST /v1/videos: queue one local clip on the existing batch executor."""
        answer=await self.front.start(request,user)
        if answer.status_code>=400:
            return answer
        identifier=json.loads(answer.body)['id']
        return JSONResponse(self.resource(user,identifier),202,headers={'cache-control':'no-store'})

    async def listing(self,request,user):
        """GET /v1/videos: list only the caller's retained and temporary video jobs."""
        for identifier,row in list(self.front.transient.items()):
            if row['status']!='in_progress' and time.time()-row.get('completed_at',row['created'])>3600:
                self.front.transient.pop(identifier,None)
                self.front.progress.pop(identifier,None)
        self.front.store.prune_videos(user.identity,self.front.limits.read('video_retention_days'))
        try:
            limit=int(request.query_params.get('limit',20))
            if not 1<=limit<=100:
                raise ValueError()
        except ValueError:
            return error(400,'limit must be an integer from 1 to 100.')
        order=request.query_params.get('order','desc')
        if order not in ('asc','desc'):
            return error(400,'order must be asc or desc.')
        after=request.query_params.get('after')
        cursor=None
        if after:
            if not self.resource(user,after):
                return error(400,'Unknown video cursor.')
            record=self.front.transient.get(after) or self.front.store.job(user.identity,after)
            cursor=(record['created'],after)
        candidates=self.front.store.video_jobs(user.identity,limit+1,after=cursor,ascending=order=='asc')
        for identifier,row in self.front.transient.items():
            key=(row['created'],identifier)
            if row.get('owner')==user.identity and row.get('kind')=='video' and (not cursor or (key>cursor if order=='asc' else key<cursor)):
                candidates.append({'id':identifier,'created':row['created']})
        candidates.sort(key=lambda row:(row['created'],row['id']),reverse=order=='desc')
        rows=[row for one in candidates if (row:=self.resource(user,one['id']))]
        headers={'cache-control':'no-store'}
        if any(row['temporary'] for row in rows[:limit]):
            observation.silence();headers['x-suite-image-temporary']='1'
        return JSONResponse({'object':'list','data':rows[:limit],'has_more':len(rows)>limit},headers=headers)

    async def item(self,request,user):
        """GET/DELETE /v1/videos/{id}: inspect or delete an owned completed clip and job."""
        identifier=request.path_params['identifier']
        row=self.resource(user,identifier)
        if not row:
            return error(404,'There is no such video of yours.')
        if row['temporary']:
            observation.silence()
        if request.method=='DELETE':
            batch=self.front.batches.store.get(identifier) if not row['temporary'] else None
            if row['status'] in ('queued','in_progress') or batch and batch['status'] in ('in_progress','paused','cancelling'):
                return error(409,'Stop the video and wait for cancellation before deleting it.')
            for asset in row['assets']:
                self.front.store.delete(user.identity,asset['id'])
            if row['temporary']:
                self.front.transient.pop(identifier,None)
            else:
                with self.front.store.db:
                    self.front.store.db.execute('DELETE FROM mirrorwall_job_assets WHERE job=?',(identifier,))
                    self.front.store.db.execute('DELETE FROM mirrorwall_jobs WHERE id=? AND owner_id=?',(identifier,user.identity))
            self.front.progress.pop(identifier,None)
            return JSONResponse({'id':identifier,'object':'video.deleted','deleted':True})
        return JSONResponse(row,headers={'cache-control':'no-store',**({'x-suite-image-temporary':'1'} if row['temporary'] else {})})

    async def content(self,request,user):
        """GET /v1/videos/{id}/content: return private WebM or its poster derivative."""
        row=self.resource(user,request.path_params['identifier'])
        if not row:
            return error(404,'There is no such video of yours.')
        if not row['asset_id']:
            return error(409,'This video has no completed content.')
        if row['temporary']:
            observation.silence()
        variant=request.query_params.get('variant','video')
        if variant not in ('video','thumbnail'):
            return error(400,'Use variant=video or thumbnail.')
        content=self.front.store.content(user.identity,row['asset_id'],variant=='thumbnail')
        if not content:
            return error(404,'This video derivative is unavailable; the browser may create its poster.')
        headers={'cache-control':'no-store','x-content-type-options':'nosniff','accept-ranges':'bytes'}
        status=200
        if requested:=request.headers.get('range'):
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
            if not match or not any(match.groups()):
                return Response(status_code=416,headers={**headers,'content-range':f'bytes */{len(content)}'})
            start=int(match[1]) if match[1] else max(0,len(content)-int(match[2]))
            end=min(int(match[2]),len(content)-1) if match[1] and match[2] else len(content)-1
            if start>end or start>=len(content):
                return Response(status_code=416,headers={**headers,'content-range':f'bytes */{len(content)}'})
            headers['content-range']=f'bytes {start}-{end}/{len(content)}'
            content=content[start:end+1];status=206
        if row['temporary']:
            headers['x-suite-image-temporary']='1'
        return Response(content,status,media_type='video/webm' if variant=='video' else 'image/png',headers=headers)

    async def poster(self,request,user):
        """Attach an owner-supplied browser-captured poster when optional ffmpeg is unavailable."""
        row=self.resource(user,request.path_params['identifier'])
        if not row or not row['asset_id']:
            return error(404,'There is no completed video of yours.')
        if row['temporary']:
            observation.silence()
        try:
            body=await self.front.read_json(request)
            if body.keys()!={'b64_json'}:
                raise ValueError('Supply only a base64 poster image.')
            self.front.store.poster(user.identity,row['asset_id'],base64.b64decode(body['b64_json'],validate=True))
            return JSONResponse({'id':row['id'],'poster_available':True},201)
        except (ValueError,TypeError,OSError) as problem:
            return error(400,str(problem))

    def routes(self,caller,log):
        """Register Videos resources behind the same existing API-key authority as images."""
        from turnstile.accounts import signed_in
        async def cancel(request,user):
            """Cancel an owned video job and await native cleanup before GPU capacity is reused."""
            if not self.resource(user,request.path_params['identifier']):
                return error(404,'There is no such video of yours.')
            return await self.front.control(request,user)
        def route(path,handler,method='GET'):
            """Use authenticated current account state for every resource operation."""
            return Route(path,signed_in(caller,log,handler),methods=[method])
        return [route('/v1/videos',self.create,'POST'),route('/v1/videos',self.listing),
                route('/v1/videos/{identifier}/content',self.content),
                route('/v1/videos/{identifier}/cancel',cancel,'POST'),
                route('/v1/videos/{identifier}/poster',self.poster,'POST'),
                route('/v1/videos/{identifier}',self.item),route('/v1/videos/{identifier}',self.item,'DELETE')]
