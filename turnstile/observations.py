"""Owner-scoped metadata lookup, export and operational reports on the existing front API."""

import asyncio
import json

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loadcoach.observation_store import search, metrics, sources, tree
from turnstile.accounts import signed_in, error
from turnstile.reporting import ReportTimeout, run


def routes(log, caller):
    """Expose bounded pure reports; correlation identifiers never replace authorization."""

    async def observations(request, user):
        """Search/export optional metadata through an independent deadline-limited reader."""
        try:
            limit = min(max(int(request.query_params.get("limit", 100)), 1), 1000)
            page = min(max(int(request.query_params.get("page", 1)), 1), 1000000)
            filters = dict(request.query_params)
            owner = None if user.can("see-log") else user.identity or user.name
            if hasattr(log, "observations"):
                await asyncio.to_thread(log.observations.flush, 0.5)
            found = await run(
                log,
                lambda view: search(view.db, owner, filters, limit, (page - 1) * limit),
            )
            found["spans"] = tree(found["spans"])
        except (ValueError, OverflowError):
            return error(
                400,
                "Choose bounded filter values, pagination and a finite increasing time range.",
            )
        except ReportTimeout:
            return error(
                503, "The observation report took too long. Choose a narrower range."
            )
        headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
        if request.query_params.get("format") == "jsonl":
            return Response(
                "".join(
                    json.dumps(row, separators=(",", ":")) + "\n"
                    for row in found["spans"]
                ),
                media_type="application/x-ndjson",
                headers=headers,
            )
        if request.query_params.get("format") == "receipt":
            if not filters.get("request_id"):
                return error(400, "A receipt requires an exact ingress request_id.")
            fields = (
                "id",
                "trace_id",
                "job_trace_id",
                "parent_id",
                "service",
                "machine",
                "operation",
                "release",
                "request_number",
                "attempt",
                "at",
                "seconds",
                "status",
                "outcome",
                "error_code",
                "route",
                "build",
                "queue_seconds",
                "readiness_seconds",
                "first_output_seconds",
                "finished",
                "uncertainty",
                "parent_unavailable",
            )
            found = {
                "version": 1,
                "request_id": filters["request_id"],
                "attempts": [
                    {key: row[key] for key in fields if key in row}
                    for row in found["spans"][:32]
                ],
                "truncated": found["truncated"] or len(found["spans"]) > 32,
                "basis": "openWeightGym observations; account/key labels omitted; remote/client receipt is not inferred.",
            }
        return JSONResponse(found, headers=headers)

    async def operational(request, user):
        """Return authorized metric windows and operator-only pipeline freshness."""
        try:
            seconds = min(
                max(int(request.query_params.get("seconds", 3600)), 60), 30 * 86400
            )
            owner = None if user.can("see-log") else user.identity or user.name
            report = await run(log, lambda view: metrics(view.db, owner, seconds))
            if user.can("see-log"):
                report["sources"] = await run(log, lambda view: sources(view.db))
                report["local"] = (
                    log.observations.health() if hasattr(log, "observations") else None
                )
        except ValueError:
            return error(400, "Window seconds must be a whole number.")
        except ReportTimeout:
            return error(503, "The observation report took too long.")
        return JSONResponse(report, headers={"cache-control": "no-store"})

    return [
        Route("/loadcoach/observations", signed_in(caller, log, observations)),
        Route("/loadcoach/observation-metrics", signed_in(caller, log, operational)),
    ]
