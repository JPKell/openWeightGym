"""Schema-driven plugin management in the existing WeightRoom process."""

from __future__ import annotations

import json
from urllib.parse import quote

from starlette.responses import Response
from starlette.routing import Route

from plugins._installer import bundle
from plugins._registry import LEVELS
from plugins._sdk import AdapterError


def build_routes(registry, controller, for_people, page, go, typed):
    """Bind plugin UI actions to WeightRoom authentication, CSRF checks and page rendering."""

    def manager(person):
        """Require the effective permission; a superuser marker cannot override an explicit removal."""
        if "front-manage-plugins" not in person.can:
            raise AdapterError("You may not manage plugins.", 403, "permission_denied")

    def principal(person):
        """Use a stable identity for activity ownership, including an authorized offline cookie."""
        return {**person.me, "name": person.name, "identity": person.me.get("identity", person.name), "can": person.can}

    def base(plugin, instance=""):
        """Build only slug-based page URLs; public model names are data, never path components."""
        return "/plugins/" + plugin + ("/instances/" + instance if instance else "")

    def fields(manifest, values):
        """Describe all schema properties for a generic settings form."""
        return [{"key": key, "schema": prop, "value": values.get(key, prop.get("default", ""))}
                for key, prop in manifest.schema.get("properties", {}).items()]

    async def home(request, person):
        """Show valid/invalid folders and enablement without executing plugin code."""
        manager(person)
        return page(request, "plugins.html", person, plugins=registry.scan(), auth_status=registry.auth.status())

    async def plugin_page(request, person, said='', failed=False, status=200):
        """Show instances and an explicit plugin enable/disable control."""
        manager(person)
        plugin = request.path_params["plugin"]
        manifest = registry.manifest(plugin)
        return page(request, "plugin.html", person, status=status, manifest=manifest, state=registry.state(plugin), said=said or request.query_params.get("said", ""), failed=failed or "failed" in request.query_params)

    async def plugin_do(request, person):
        """Change plugin enablement locally; turnstile reads it on the next request."""
        manager(person)
        form = await typed(request)
        registry.enable(request.path_params["plugin"], form.get("enabled") == "on")
        return go(base(request.path_params["plugin"]) + "?said=Saved.")

    async def instance_page(request, person, said='', failed=False, status=200):
        """Show plugin-specific settings, model aliases, sync revisions and health controls."""
        manager(person)
        plugin = request.path_params["plugin"]
        instance = request.path_params.get("instance", "")
        manifest = registry.manifest(plugin)
        config = registry.instance(plugin, instance) if instance else {
            "settings": {}, "enabled": False, "url": "http://127.0.0.1:8450", "models": {}, "health": {},
            "revision": 0, "applied_revision": 0, "max_sensitivity": "normal"}
        return page(request, "plugin_instance.html", person, status=status, manifest=manifest, instance=instance,
                    config=config, fields=fields(manifest, config["settings"]), levels=LEVELS,
                    said=said or request.query_params.get("said", ""), failed=failed or "failed" in request.query_params)

    async def instance_do(request, person):
        """Save pending settings or perform one explicitly selected capability operation."""
        manager(person)
        plugin = request.path_params["plugin"]
        form = await typed(request)
        instance = request.path_params.get("instance") or form.get("instance", "").strip()
        action = form.get("do", "save")
        said = "Saved."
        if action == "save":
            manifest = registry.manifest(plugin)
            values = {}
            for key, prop in manifest.schema.get("properties", {}).items():
                kind = prop.get("type")
                raw = form.get("setting-" + key, "")
                try:
                    if kind == "boolean":
                        values[key] = raw == "on"
                    elif kind == "string":
                        values[key] = raw
                    elif raw.strip():
                        values[key] = json.loads(raw)
                    elif "default" in prop:
                        values[key] = prop["default"]
                except ValueError:
                    raise AdapterError(key + ": expected JSON or a number.", 400, "invalid_settings") from None
            try:
                interval = int(form.get("health_interval") or 30)
            except ValueError:
                raise AdapterError("Health interval must be an integer.", 400, "invalid_settings") from None
            policy = {key: form.get("health-" + key) == "on" for key in ("enabled", "allow_start", "allow_restart", "auto_start", "auto_restart")}
            policy["interval"] = interval
            registry.save_instance(plugin, instance, form.get("url", ""), values, enabled=form.get("enabled") == "on",
                                   health=policy, sensitivity=form.get("sensitivity", "normal"))
            try:
                await controller.sync(plugin, instance)
                said = "Saved locally and applied by the adapter."
            except AdapterError:
                said = "Saved locally. Remote settings are pending; turnstile will synchronize on reconnection."
        elif action == "model":
            registry.model(plugin, instance, form.get("profile", ""), form.get("alias", "").strip(), form.get("enabled") == "on")
        elif action == "add-model":
            registry.add_model(plugin, instance, form.get("profile", "").strip())
        elif action == "discover":
            await controller.discover(plugin, instance)
            said = "Profiles/models refreshed. New entries start disabled."
        elif action == "sync":
            await controller.sync(plugin, instance)
            said = "Remote settings synchronized."
        elif action == "health":
            await controller.health(plugin, instance)
            said = "Health checked."
        elif action in ("start", "restart"):
            await controller.control(plugin, instance, action)
            said = "Requested Hermes/service " + action + "."
        else:
            raise AdapterError("Unknown plugin action.", 400, "invalid_action")
        return go(base(plugin, instance) + "?said=" + quote(said))

    async def installer(request, person):
        """Download a private, instance-paired Linux bundle; never place it in static assets."""
        manager(person)
        plugin, instance = request.path_params["plugin"], request.path_params["instance"]
        return Response(bundle(registry, plugin, instance), media_type="application/gzip",
            headers={"content-disposition": f'attachment; filename="suite-{plugin}-{instance}-linux.tar.gz"', "cache-control": "no-store"})

    async def activity(request, person):
        """Show minimal adaptor records under the caller's own identity or see-log permission."""
        if not set(person.can) & {"front-plugins", "front-manage-plugins", "see-log"}:
            raise AdapterError("You may not read adaptor activity.", 403, "permission_denied")
        from weightroom.pagination import controls, pagination
        limit, current = controls(request.query_params)
        user = principal(person)
        total = registry.activity_count(user)
        current = min(current, max(1, (total + limit - 1) // limit))
        return page(request, "plugin_activity.html", person, uses=registry.activity(user, limit, (current - 1) * limit),
                    paging=pagination('/plugins/activity', request.query_params, total, limit, current))

    def route(path, handler, methods=None):
        """Retain WeightRoom's cookie/CSRF checks and make authorized plugin management work offline."""
        async def checked(request, person):
            """Translate plugin validation failures into the existing operator form response."""
            try:
                return await handler(request, person)
            except AdapterError as problem:
                if request.method == "POST" and problem.code != 'permission_denied':
                    try:
                        showing = instance_page if handler is instance_do else plugin_page
                        return await showing(request, person, said=str(problem), failed=True, status=problem.status)
                    except AdapterError:
                        pass
                return page(request, "refused.html", person, status=problem.status, why=str(problem))
        return Route(path, for_people(checked), methods=methods or ["GET"])

    return [route("/plugins", home), route("/plugins/activity", activity),
            route("/plugins/{plugin}", plugin_page), route("/plugins/{plugin}", plugin_do, ["POST"]),
            route("/plugins/{plugin}/instances/new", instance_page), route("/plugins/{plugin}/instances/new", instance_do, ["POST"]),
            route("/plugins/{plugin}/instances/{instance}", instance_page), route("/plugins/{plugin}/instances/{instance}", instance_do, ["POST"]),
            route("/plugins/{plugin}/instances/{instance}/installer", installer)]
