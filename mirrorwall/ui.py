"""Browser pages and narrow authenticated proxies into openWeightGym's existing APIs."""
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach import observability as observation
from mirrorwall.front import Front
from mirrorwall.model import check_name


def routes(for_people, page, ask, model_api):
    """Keep browser credentials server-side and reuse the ordinary session authority."""
    async def studio(request, person):
        """Render the private image-generation window."""
        return page(request, "mirrorwall_studio.html", person)

    async def workflows(request, person):
        """Render versioned image workflow settings."""
        return page(request, "mirrorwall_workflows.html", person, studio_manage="manage-models" in person.can)

    async def models(request, person):
        """Render image-only model management for authorized administrators."""
        if "manage-models" not in person.can:
            return page(request, "refused.html", person, status=403, why="Your account may not manage image models.")
        return page(request, "mirrorwall_models.html", person, studio_manage=True)

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
                     (len(parts) == 2 and parts[0] == "presets" and request.method == "GET") or
                     (action in ("assets", "jobs", "presets", "preferences") and request.method == "POST") or
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
        answer = await ask(request.method, "/mirrorwall/" + action, person.key,
                           params=dict(request.query_params), **more)
        if answer.headers.get("x-suite-image-temporary") == "1":
            observation.silence()
        headers = {key: value for key, value in answer.headers.items()
                   if key.lower() in ("content-type", "content-disposition", "x-suite-image-temporary")}
        headers.update({"cache-control": "no-store", "x-content-type-options": "nosniff"})
        return Response(answer.content, answer.status_code, headers=headers)

    async def inventory(request, person):
        """Read the selected node image catalogue and runtime availability."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        answer = await model_api(request, person, "GET", "/loadcoach/models")
        if answer.is_error:
            return Response(answer.content, answer.status_code, media_type="application/json")
        value = answer.json()
        value['cards'] = value.get('engine_cards', {}).get('sdcpp', value.get('cards', []))
        value["models"] = [one for one in value["models"] if one.get("engine") == "sdcpp"]
        selected_node=request.query_params.get('node') or 'compute'
        engines = await ask('GET','/turnstile/machines/'+quote(selected_node,safe='')+'/engines',person.key)
        value["engines"] = engines.json() if engines.is_success else {}
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
        """Render image source discovery for model administrators."""
        if 'manage-models' not in person.can:
            return page(request,'refused.html',person,status=403,why='Model management is required.')
        preferences=await ask('GET','/mirrorwall/preferences',person.key)
        value=preferences.json() if preferences.is_success else {}
        return page(request,'mirrorwall_discovery.html',person,
                    adult_previews_allowed=value.get('adult_previews_allowed',False),
                    adult_previews=value.get('adult_previews',False))

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

    return [Route("/chat/images", for_people(studio)),
            Route("/models/images", for_people(models)),
            Route("/models/images/workflows", for_people(workflows)),
            Route("/mirrorwall-models", for_people(inventory)),
            Route("/mirrorwall-models", for_people(model_save), methods=["POST"]),
            Route("/mirrorwall-models/check", for_people(model_check), methods=["POST"]),
            Route('/civitai',for_people(discovery_page)),
            Route('/mirrorwall-discovery',for_people(discovery),methods=['GET','POST']),
            Route("/mirrorwall-api/{action:path}", for_people(proxy), methods=["GET", "POST", "DELETE"])]
