"""Bookmarkable Plugin logs filters, retained-record exports and view links."""

import csv
import io
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

from plugins._registry import ACTIVITY_SORTS

FIELDS = ("id", "at", "user", "model", "plugin", "instance", "profile", "outcome", "request", "session")


def options(params, now=None):
    """Normalize supported controls; filters always run inside the registry's ownership scope."""
    period = params.get("period", "all")
    period = period if period in ("all", "24", "168") else "all"
    values = {key: params.get(key, "").strip()[:500] for key in ("q", "user", "instance", "outcome", "session")}
    values.update(period=period, sort=params.get("sort", "at"), direction=params.get("direction", "desc"))
    if values["sort"] not in ACTIVITY_SORTS:
        values["sort"] = "at"
    if values["direction"] not in ("asc", "desc"):
        values["direction"] = "desc"
    query = {key: values[key] for key in ("q", "user", "instance", "outcome", "session") if values[key]}
    if period != "all":
        query["since"] = (time.time() if now is None else now) - int(period) * 3600
    return values, query


def activity_url(params, **changes):
    """Retain filters across links while dropping transient selection and export controls."""
    query = {key: params[key] for key in ("q", "user", "instance", "outcome", "session", "period", "sort", "direction", "limit", "page")
             if params.get(key) not in (None, "", "all")}
    query.update(changes)
    query = {key: value for key, value in query.items() if value is not None and value != ""}
    return "/plugins/activity" + ("?" + urlencode(query) if query else "")


def public_record(record):
    """Keep the internal stable owner identity out of presentation and exports."""
    return {field: record[field] for field in FIELDS}


def csv_lines(records):
    """Stream exact filtered metadata with UTC timestamps and safe spreadsheet cells."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def line(values):
        """Encode one CSV row and reuse the bounded output buffer."""
        buffer.seek(0)
        buffer.truncate(0)
        safe = []
        for value in values:
            text = str(value) if value is not None else ""
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)
        return buffer.getvalue()

    yield line(FIELDS)
    for record in records:
        yield line(datetime.fromtimestamp(record[field], UTC).isoformat() if field == "at" else record[field] for field in FIELDS)
