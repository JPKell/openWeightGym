"""SoundRoom browser pages and narrow authenticated proxies into openWeightGym's existing APIs."""
from contextlib import suppress
from urllib.parse import parse_qs, quote, urlsplit

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from loadcoach import observability as observation
from soundroom.front import Front
from loadcoach.platform_services import PlatformServices


def routes(for_people, page, ask, model_api, settings=None, platform=None):
    """Keep browser credentials server-side and reuse the ordinary session authority."""
    services = platform or (lambda: PlatformServices.from_settings(settings))
    def defaults():
        """The administrator's first choices for the audio pages."""
        return {kind: getattr(settings, "audio_" + kind + "_model", "") for kind in ("lyrics", "music", "sound", "speech", "transcription")}

    async def music(request, person):
        """Render the private music window."""
        return page(request, "soundroom_music.html", person, mode="music", audio=defaults())

    async def sounds(request, person):
        """Render the private sound-effects window."""
        return page(request, "soundroom_music.html", person, mode="sound", audio=defaults())

    async def speech(request, person):
        """Render private voice generation and the saved voice library."""
        return page(request, "soundroom_speech.html", person, mode="speech", audio=defaults())

    async def transcribe(request, person):
        """Render the independent authenticated transcription workspace."""
        return page(request, "soundroom_transcribe.html", person, audio=defaults())

    async def preferences(request, person):
        """Render the person's own audio choices."""
        return page(request, "soundroom_preferences.html", person)

    DEFAULTS = (("audio_lyrics_model", "Lyrics helper model", "A chat model or role, such as chat, that writes the style caption and lyrics."),
                ("audio_music_model", "Music page model", "Selected first on Chat → Music; empty selects the first available."),
                ("audio_sound_model", "Sounds page model", "Selected first on Chat → Sounds."),
                ("audio_speech_model", "Voice generation model", "Selected first on Chat → Voice generation."),
                ("audio_transcription_model", "Transcription model", "Selected first on Chat → Transcribe."))

    async def system(request, person):
        """Show and change the administrator's audio defaults, kept with weightroom's own settings."""
        if "manage-settings" not in person.can:
            return page(request, "refused.html", person, status=403, why="Your account may not change settings.")
        from weightroom.app import ServiceUnavailable
        found = None
        with suppress(ServiceUnavailable):
            found = await ask("GET", "/loadcoach/settings", person.key)
        settings_now = next((one["settings"] for one in found.json().get("programs", []) if one["program"] == "weightroom"), []) if found is not None and found.is_success else []
        current = {one["name"]: one for one in settings_now}
        available_defaults = [field for field in DEFAULTS if
                              (services().speech if field[0] in ('audio_speech_model', 'audio_transcription_model') else services().audio)]
        said, failed = request.query_params.get("said", ""), False
        if request.method == "POST":
            body = await request.body()
            if len(body) > 65536:
                return page(request, "refused.html", person, status=413, why="The form is too large.")
            form = {name: values[-1] for name, values in parse_qs(body.decode(errors="replace")).items()}
            asked = {}
            for name, _, _ in available_defaults:
                one = current.get(name)
                holds = one["changed"]["value"] if one and one.get("changed") else one["value"] if one else ""
                typed = str(form.get("value-" + name, holds)).strip()
                if one and one.get("when") != "file" and typed != holds:
                    asked[name] = typed
            if not asked:
                return RedirectResponse("/system/audio?said=Nothing+was+changed.", 303)
            answer = await ask("POST", "/loadcoach/settings/weightroom", person.key, json=asked)
            if answer.is_success:
                return RedirectResponse("/system/audio?said=Saved.+Applied+when+weightroom+restarts.", 303)
            said, failed = answer.json().get("error", {}).get("message", "Not saved."), True
        listed = await ask("GET", "/turnstile/machines", person.key)
        nodes = []
        for name in [one["name"] for one in listed.json().get("machines", [])] if listed.is_success else []:
            # Each node says whether its audio engine is there; one not answering says why not.
            engines = await ask("GET", "/turnstile/machines/" + quote(name, safe="") + "/engines", person.key)
            audio = next((one for one in (engines.json().get("engines", []) if engines.is_success else []) if one.get("id") == "audiocpp"), None)
            nodes.append({"name": name, "available": bool(audio and audio.get("available")), "why": (audio or {}).get("why", "not answering")})
        fields = [{"name": name, "label": label, "help": help, "when": current.get(name, {}).get("when", ""),
                   "value": (current[name]["changed"]["value"] if current.get(name, {}).get("changed") else current.get(name, {}).get("value", ""))}
                  for name, label, help in available_defaults]
        catalogue = None
        with suppress(ServiceUnavailable):
            catalogue = await ask('GET', '/v1/models', person.key)
        available = catalogue.json().get('data', []) if catalogue is not None and catalogue.is_success else []
        for field in fields:
            kind = field['name'].removeprefix('audio_').removesuffix('_model')
            kinds = {'speech': ('speech', 'clone', 'design')}.get(kind, (kind,))
            field['choices'] = [row['id'] for row in available
                                if (row.get('audio_profile', {}).get('kind') in kinds
                                    if kind != 'lyrics' else not row.get('audio_profile') and not row.get('image_workflow')
                                    and not row.get('capabilities', {}).get('embedding'))]
        return page(request, "soundroom_system.html", person, fields=fields, nodes=nodes, said=said, failed=failed)

    async def models(request, person):
        """Render audio-only model management for authorized administrators."""
        if "manage-models" not in person.can:
            return page(request, "refused.html", person, status=403, why="Your account may not manage audio models.")
        answer = await ask("GET", "/turnstile/machines", person.key)
        nodes = answer.json().get("machines", []) if answer.is_success else []
        return page(request, "soundroom_models.html", person,
                    audio_nodes=[{key: node[key] for key in ("name", "state", "accepting_work") if key in node}
                                 for node in nodes])

    async def proxy(request, person):
        """Forward only allowlisted audio operations using the current server-side session."""
        action = request.path_params["action"]
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": {"message": "Use this site's own audio page."}}, 403)
        # Explicit shapes keep this proxy from becoming a general management API.
        parts = action.split("/")
        permitted = ((action in ("state", "preferences") and request.method == "GET") or
                     (action in ("assets", "jobs", "voices", "preferences", "lyrics", "transcribe") and request.method == "POST") or
                     (len(parts) == 2 and parts[0] in ("jobs", "assets") and request.method == "POST") or
                     (len(parts) == 2 and parts[0] in ("assets", "voices") and request.method == "DELETE") or
                     (len(parts) == 3 and parts[0] == "voices" and parts[2] == "share" and request.method == "POST") or
                     (len(parts) == 3 and parts[0] == "assets" and parts[2] == "content" and request.method in ("GET", "HEAD")))
        if not permitted:
            return JSONResponse({"error": {"message": "Unknown audio operation."}}, 404)
        more = {}
        if len(parts) == 3 and parts[0] == 'assets' and parts[2] == 'content' and request.headers.get('range'):
            more['headers'] = {'range': request.headers['range']}
        if request.method == "POST":
            try:
                more["json"] = await Front.read_json(request)
            except (ValueError, TypeError) as problem:
                return JSONResponse({"error": {"message": str(problem)}}, 400)
            if more["json"].get("temporary"):
                if "unlogged" not in person.can:
                    return JSONResponse({"error": {"message": "Your account may not use no-log audio."}}, 403)
                observation.silence()
        answer = await ask(request.method, "/soundroom/" + action, person.key, params=dict(request.query_params), **more)
        if answer.headers.get("x-suite-audio-temporary") == "1":
            observation.silence()
        headers = {key: value for key, value in answer.headers.items()
                   if key.lower() in ("content-type", "content-disposition", "content-length", "accept-ranges", "content-range", "x-suite-audio-temporary")}
        headers.update({"cache-control": "no-store", "x-content-type-options": "nosniff"})
        return Response(answer.content, answer.status_code, headers=headers)

    async def inventory(request, person):
        """The selected node's audio models, runtime availability and pinned catalogue."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        answer = await model_api(request, person, "GET", "/loadcoach/models")
        if answer.is_error:
            return Response(answer.content, answer.status_code, media_type="application/json")
        value = answer.json()
        value['cards'] = value.get('engine_cards', {}).get('audiocpp', value.get('cards', []))
        value["models"] = [one for one in value["models"] if one.get("engine") == "audiocpp"]
        value['models'] = [one for one in value['models'] if services().model_enabled(one)]
        engines = await model_api(request, person, "GET", "/loadcoach/engines")
        value["engines"] = engines.json() if engines.is_success else {}
        packages = await model_api(request, person, "GET", "/loadcoach/audio/families")
        value["catalogue"] = packages.json() if packages.is_success else {"families": [], "error": packages.text[:300]}
        value['catalogue']['families'] = [{**family, 'kinds': [kind for kind in family.get('kinds', []) if services().audio_task_enabled(kind)]}
                                          for family in value['catalogue'].get('families', [])]
        return JSONResponse(value, headers={"cache-control": "no-store"})

    async def installations(request, person):
        """Report the requested installation jobs without leaving the Audio studio."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        wanted = set(request.query_params.getlist('install'))
        if len(wanted) > 1000:
            return JSONResponse({"error": {"message": "Too many installation jobs."}}, 400)
        if not wanted:
            return JSONResponse({"installations": []})
        answer = await ask('GET', '/turnstile/artifacts/distributions', person.key)
        if answer.is_error:
            return Response(answer.content, answer.status_code, media_type="application/json")
        fields = {'id', 'name', 'machine', 'state', 'file', 'done', 'of', 'why'}
        jobs = [{key: value for key, value in job.items() if key in fields}
                for job in answer.json().get('distributions', [])
                if job.get('id') in wanted and job.get('kind') == 'model-install']
        return JSONResponse({"installations": jobs}, headers={"cache-control": "no-store"})

    async def model_save(request, person):
        """Validate the audio envelope before updating the selected compute node."""
        if "manage-models" not in person.can:
            return JSONResponse({"error": {"message": "Model management is required."}}, 403)
        try:
            body = await Front.read_json(request)
            name, settings = body["name"], body["settings"]
            if not isinstance(name, str) or not isinstance(settings, dict) or settings.get("engine") != "audiocpp":
                raise ValueError("Choose an audio model name and settings.")
            if not services().model_enabled(settings):
                return JSONResponse({'error': {'message': 'This audio task is disabled in Platform services.'}}, 403)
            destinations = body.get('machines')
            if destinations is not None and (not body.get('new') or not isinstance(destinations, list) or not destinations
                or len(destinations) > 100 or any(not isinstance(node, str) or not node.strip() for node in destinations)):
                raise ValueError('Choose at least one destination node when adding an audio model.')
            if destinations is not None:
                destinations = list(dict.fromkeys(destinations))
        except (ValueError, KeyError, TypeError) as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        answer = await model_api(request, person, "POST", "/loadcoach/models/" + quote(name, safe="/:"),
                                 json=settings, params={"new": "1"} if body.get("new") else {},
                                 prepare_files=bool(body.get("new")), source_machine=body.get("source_machine", ""), machines=destinations)
        return Response(answer.content, answer.status_code, media_type="application/json", headers={"cache-control": "no-store"})

    return [Route("/chat/music", for_people(music)),
            Route("/chat/sounds", for_people(sounds)),
            Route("/chat/speech", for_people(speech)),
            Route("/chat/transcribe", for_people(transcribe)),
            Route("/account/audio", for_people(preferences)),
            Route("/system/audio", for_people(system), methods=["GET", "POST"]),
            Route("/models/audio", for_people(models)),
            Route("/soundroom-models", for_people(inventory)),
            Route("/soundroom-models", for_people(model_save), methods=["POST"]),
            Route("/soundroom-installations", for_people(installations)),
            Route("/soundroom-api/{action:path}", for_people(proxy), methods=["GET", "POST", "DELETE"])]
