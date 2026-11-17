"""Browser pages and narrow authenticated proxies into openWeightGym's existing APIs."""
import asyncio
import io
import zipfile
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach import observability as observation
from mirrorwall.civitai import BASE_MODEL_GROUPS, MODEL_TYPE_CHOICES
from mirrorwall.front import Front
from mirrorwall.model import check_name, SAMPLERS, SCHEDULERS
from mirrorwall.store import ASSET_ID


async def image_archive(ask, person, asked):
    """Download up to 200 owned images as a ZIP, bounded to 256 MiB of source data.

    Every content read uses the existing owner-scoped API. Archives stay in memory so
    temporary no-log images never acquire an on-disk export.
    """
    identifiers = asked.get('ids')
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 200 or
        any(not isinstance(one, str) or not ASSET_ID.fullmatch(one) for one in identifiers)):
        return JSONResponse({'error': {'message': 'Select between 1 and 200 images to download.'}}, 400)
    headers = {'content-disposition': 'attachment; filename="images.zip"',
               'cache-control': 'private, no-store', 'x-content-type-options': 'nosniff'}
    total = 0
    with io.BytesIO() as content:
        with zipfile.ZipFile(content, 'w', compression=zipfile.ZIP_STORED) as archive:
            for identifier in dict.fromkeys(identifiers):
                answer = await ask('GET', '/mirrorwall/assets/' + identifier + '/content', person.key)
                if answer.headers.get('x-suite-image-temporary') == '1':
                    observation.silence()
                    headers['x-suite-image-temporary'] = '1'
                if not answer.is_success:
                    return Response(answer.content, answer.status_code, media_type='application/json',
                                    headers={'cache-control': 'private, no-store'})
                suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}.get(
                    answer.headers.get('content-type', '').split(';')[0])
                if suffix is None:
                    return JSONResponse({'error': {'message': 'Select image assets for this download.'}}, 400)
                total += len(answer.content)
                if total > 256 * 2**20:
                    return JSONResponse({'error': {'message': 'Selected images exceed 256 MiB. Download a smaller selection.'}}, 400)
                archive.writestr(identifier + suffix, answer.content)
        return Response(content.getvalue(), media_type='application/zip', headers=headers)


def routes(for_people, page, ask, model_api):
    """Keep browser credentials server-side and reuse the ordinary session authority."""
    async def studio(request, person):
        """Render the private image-generation window."""
        return page(request, "mirrorwall_studio.html", person, image_samplers=SAMPLERS, image_schedulers=SCHEDULERS)

    async def workflows(request, person):
        """Render versioned image workflow settings."""
        return page(request, "mirrorwall_workflows.html", person, studio_manage="manage-models" in person.can)

    async def models(request, person):
        """Render image-only model management for authorized administrators."""
        if "manage-models" not in person.can:
            return page(request, "refused.html", person, status=403, why="Your account may not manage image models.")
        video=request.url.path.startswith('/models/video')
        from mirrorwall.video import FAMILIES, defaults, limits
        profiles={family:{'label':family,'defaults':defaults({'sdcpp':{'family':family}}),'limits':limits({})} for family in FAMILIES}
        return page(request, "mirrorwall_models.html", person, studio_manage=True,
                    media_kind='video' if video else 'image',video_profiles=profiles if video else {},
                    samplers=SAMPLERS,schedulers=SCHEDULERS)

    async def videos(request,person):
        """Render the private video Canvas with capability-filtered model and input controls."""
        return page(request,'video_studio.html',person,video_samplers=SAMPLERS,video_schedulers=SCHEDULERS)

    async def video_proxy(request,person):
        """Forward allowlisted private video operations with the current server-side session key."""
        action=request.path_params.get('action','state')
        if request.method!='GET':
            origin=request.headers.get('origin')
            if origin and urlsplit(origin).netloc!=request.headers.get('host'):
                return JSONResponse({'error':{'message':"Use this site's own video page."}},403)
        parts=action.split('/')
        if action=='state' and request.method=='GET':
            models,jobs=await asyncio.gather(ask('GET','/v1/models',person.key),ask('GET','/v1/videos',person.key,params={'limit':100}))
            if not models.is_success or not jobs.is_success:
                return JSONResponse({'error':{'message':'Video inventory or history is unavailable.'}},503)
            if jobs.headers.get('x-suite-image-temporary')=='1':
                observation.silence()
            return JSONResponse({'models':[one for one in models.json().get('data',[]) if one.get('capabilities',{}).get('video_generation')],
                                 'jobs':jobs.json().get('data',[]),'history_has_more':jobs.json().get('has_more',False),
                                 'can_unlogged':'unlogged' in person.can},headers={'cache-control':'no-store'})
        target=None
        if action=='jobs' and request.method in ('GET','POST'):
            target='/v1/videos'
        elif len(parts)==2 and parts[0]=='jobs' and request.method in ('GET','DELETE'):
            target='/v1/videos/'+quote(parts[1],safe='')
        elif len(parts)==3 and parts[0]=='jobs' and parts[2] in ('content','cancel','poster') and request.method==('GET' if parts[2]=='content' else 'POST'):
            target='/v1/videos/'+quote(parts[1],safe='')+'/'+parts[2]
        elif action=='assets' and request.method=='POST':
            target='/mirrorwall/assets'
        elif len(parts)==3 and parts[0]=='assets' and parts[2]=='content' and request.method=='GET':
            target='/mirrorwall/assets/'+quote(parts[1],safe='')+'/content'
        if not target:
            return JSONResponse({'error':{'message':'Unknown video operation.'}},404)
        more={}
        if request.method=='POST':
            try:
                more['json']=await Front.read_json(request)
            except (ValueError,TypeError) as problem:
                return JSONResponse({'error':{'message':str(problem)}},400)
            if more['json'].get('temporary'):
                if 'unlogged' not in person.can:
                    return JSONResponse({'error':{'message':'Your account may not use no-log video.'}},403)
                observation.silence()
        if request.headers.get('range'):
            more['headers']={'range':request.headers['range']}
        answer=await ask(request.method,target,person.key,params=dict(request.query_params),**more)
        if answer.headers.get('x-suite-image-temporary')=='1':
            observation.silence()
        headers={key:value for key,value in answer.headers.items() if key.lower() in
                 ('content-type','content-disposition','accept-ranges','content-range','x-suite-image-temporary')}
        return Response(answer.content,answer.status_code,headers={**headers,'cache-control':'no-store','x-content-type-options':'nosniff'})

    async def proxy(request, person):
        """Forward only allowlisted image operations using the current server-side session."""
        action = request.path_params["action"]
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": {"message": "Use this site's own image page."}}, 403)
        # Explicit shapes prevent this proxy from becoming a general management API.
        parts = action.split("/")
        permitted = ((action in ("state", "preferences", "assets", "workflows") and request.method == "GET") or
                     (len(parts) == 2 and parts[0] == "presets" and request.method in ("GET", "DELETE")) or
                     (action in ("assets", "assets/download", "jobs", "presets", "preferences") and request.method == "POST") or
                     (len(parts) == 2 and parts[0] == "jobs" and request.method == "POST") or
                     (len(parts) == 2 and parts[0] == "assets" and request.method == "DELETE") or
                     (len(parts) == 3 and parts[0] == "assets" and parts[2] in ("content", "thumbnail") and request.method == "GET"))
        if not permitted:
            return JSONResponse({"error": {"message": "Unknown image operation."}}, 404)
        more = {}
        if request.method == "POST":
            try:
                more["json"] = await Front.read_json(request)
            except (ValueError, TypeError) as problem:
                return JSONResponse({"error": {"message": str(problem)}}, 400)
            if more["json"].get("temporary"):
                if "unlogged" not in person.can:
                    return JSONResponse({"error": {"message": "Your account may not use no-log images."}}, 403)
                observation.silence()
            if action == 'assets/download':
                return await image_archive(ask, person, more['json'])
        answer = await ask(request.method, "/mirrorwall/" + action, person.key,
                           params=dict(request.query_params), **more)
        if answer.headers.get("x-suite-image-temporary") == "1":
            observation.silence()
        headers = {key: value for key, value in answer.headers.items()
                   if key.lower() in ("content-type", "content-disposition", "x-suite-image-temporary")}
        headers.update({"cache-control": "no-store", "x-content-type-options": "nosniff"})
        return Response(answer.content, answer.status_code, headers=headers)

    async def inventory(request, person):
        """List image setups across nodes while retaining each copy's settings and identity."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        answer = await ask('GET', '/turnstile/models', person.key)
        if answer.is_error:
            return Response(answer.content, answer.status_code, media_type="application/json")
        value = answer.json()
        facts = value.get('nodes', {})
        value['node_cards'] = {name: (one.get('engine_cards') or {}).get('sdcpp', one.get('cards') or [])
                               for name, one in facts.items()}
        value['default_node'] = next(iter(facts), '')
        selected_node = request.query_params.get('node', '')
        video=request.url.path.startswith('/video-models')
        value['models'] = [{**copy, 'node':copy.get('machine', ''),
                            'workflow_model':one['name'], 'workflow_model_id':one.get('model_id')}
                           for one in value['models'] if one.get('engine') == 'sdcpp'
                           for copy in one.get('locations') or [one]
                           if (copy.get('sdcpp',one.get('sdcpp',{})).get('mode')=='vid_gen')==video
                           if not selected_node or copy.get('machine', '') == selected_node]
        value['cards'] = value['node_cards'].get(selected_node or value['default_node'],
                            (value.get('engine_cards') or {}).get('sdcpp', value.get('cards') or []))
        names = list(facts) or [value['default_node']]
        engines = await asyncio.gather(*(ask('GET','/turnstile/machines/'+quote(name,safe='')+'/engines',person.key) for name in names))
        value['node_engines'] = {name: answer.json() if answer.is_success else {} for name, answer in zip(names, engines)}
        value['engines'] = value['node_engines'].get(selected_node or value['default_node'], {})
        nodes = await ask("GET", "/turnstile/machines", person.key)
        value["nodes"] = nodes.json().get("machines", []) if nodes.is_success else []
        return JSONResponse(value, headers={"cache-control": "no-store"})

    async def model_save(request, person):
        """Validate the image envelope before updating the selected compute node."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        try:
            body = await Front.read_json(request)
            name, settings = body["name"], body["settings"]
            if not isinstance(name, str) or not isinstance(settings,dict) or settings.get("engine") != "sdcpp":
                raise ValueError("Choose an image model name and settings.")
            check_name(name)
            from mirrorwall.video import is_video
            if is_video(settings)!=request.url.path.startswith('/video-models'):
                raise ValueError('Use the model studio for this media kind.')
            destinations=body.get('machines')
            if destinations is not None and (not body.get('new') or not isinstance(destinations,list) or not destinations
                or len(destinations)>100 or any(not isinstance(node,str) or not node.strip() for node in destinations)):
                raise ValueError('Choose at least one destination node when adding an image model.')
            if destinations is not None:
                destinations=list(dict.fromkeys(destinations))
        except (ValueError, KeyError, TypeError) as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        answer = await model_api(request, person, "POST", "/loadcoach/models/" + quote(name, safe=""),
                                 json=settings, params={"new": "1"} if body.get("new") else {},
                                 prepare_files=bool(body.get("new")), source_machine=body.get("source_machine", ""), machines=destinations)
        return Response(answer.content, answer.status_code, media_type="application/json", headers={"cache-control": "no-store"})

    async def model_check(request, person):
        """Check new bundles across the library, or existing profiles on their selected node."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        try:
            body = await Front.read_json(request)
            if not isinstance(body.get("settings"), dict):
                raise ValueError("Send the draft image settings.")
        except (ValueError, TypeError) as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        if body.get('new'):
            answer = await ask('POST', '/turnstile/models/image-check', person.key,
                               json={'settings':body['settings'], 'source_machine':body.get('source_machine', '')})
        else:
            answer = await model_api(request, person, "POST", "/loadcoach/images/check", json={"settings": body["settings"]})
        return Response(answer.content, answer.status_code, media_type="application/json", headers={"cache-control": "no-store"})

    async def discovery_page(request, person):
        """Render image and video source discovery for model administrators."""
        if 'manage-models' not in person.can:
            return page(request,'refused.html',person,status=403,why='Model management is required.')
        return page(request,'mirrorwall_discovery.html',person,civitai_base_model_groups=BASE_MODEL_GROUPS,
                    civitai_model_type_choices=MODEL_TYPE_CHOICES)

    async def discovery(request,person):
        """Search or download Civitai versions through the existing artifact authority."""
        if 'manage-models' not in person.can:
            return JSONResponse({'error':{'message':'Model management is required.'}},403)
        if request.method=='POST':
            try:
                body=await Front.read_json(request)
                if not str(body.get('repository','')).startswith('civitai/'):
                    raise ValueError('Choose a Civitai version.')
            except ValueError as problem:
                return JSONResponse({'error':{'message':str(problem)}},400)
            answer=await ask('POST','/turnstile/artifacts/downloads',person.key,json=body)
        else:
            preferences=await ask('GET','/mirrorwall/preferences',person.key)
            adult=preferences.is_success and preferences.json().get('adult_previews',False)
            repository=request.query_params.get('repository','')
            if repository and not repository.startswith('civitai/'):
                return JSONResponse({'error':{'message':'Choose a Civitai version.'}},400)
            answer=await ask('GET','/turnstile/artifacts/hub',person.key,
                             params={'repository':repository,'search':request.query_params.get('q',''),
                                     'source':'civitai','adult':str(bool(adult)).lower(),
                                     **{key:request.query_params[key] for key in ('paged','cursor','kind','base') if key in request.query_params}})
        return Response(answer.content,answer.status_code,media_type='application/json',headers={'cache-control':'no-store'})

    return [Route('/chat/video',for_people(videos)),Route('/models/video',for_people(models)),
            Route('/video-api',for_people(video_proxy)),
            Route('/video-api/{action:path}',for_people(video_proxy),methods=['GET','POST','DELETE']),
            Route('/video-models',for_people(inventory)),Route('/video-models',for_people(model_save),methods=['POST']),
            Route('/video-models/check',for_people(model_check),methods=['POST']),
            Route("/chat/images", for_people(studio)),
            Route("/models/images", for_people(models)),
            Route("/models/images/workflows", for_people(workflows)),
            Route("/mirrorwall-models", for_people(inventory)),
            Route("/mirrorwall-models", for_people(model_save), methods=["POST"]),
            Route("/mirrorwall-models/check", for_people(model_check), methods=["POST"]),
            Route('/civitai',for_people(discovery_page)),
            Route('/mirrorwall-discovery',for_people(discovery),methods=['GET','POST']),
            Route("/mirrorwall-api/{action:path}", for_people(proxy), methods=["GET", "POST", "DELETE"])]
