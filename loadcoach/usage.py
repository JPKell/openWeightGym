"""Token reports from retained request endings, with calendar days in the caller's timezone.

No prompt text is kept. Each request ending is also counted into `usage_hours`, one row per UTC
hour and user, group, model, service, outcome and sensitivity level, in the same transaction that
appends it to the log. Reports read those rows, which stay after old events are pruned; a
timezone whose offset is not whole hours reads the endings themselves. The caller supplies an enforced owner scope;
filters and their choices can only narrow that scope. New endings retain their original group,
while older ones use current membership and are counted as inferred in the report.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DIMENSIONS = ("user", "group", "model", "source", "outcome", "level")
HOUR = 3600
# What a usage_hours row adds up. `unpriced` endings recorded no cost; `complete` ones among them
# reported both token counts, so a model's rates can price them when the report is made.
SUMMED = ("requests", "input_tokens", "output_tokens", "missing_usage", "recorded_cost",
          "unpriced_requests", "unpriced_input", "unpriced_output",
          "complete_requests", "complete_input", "complete_output")
KEY = ("hour", "person", "grp", "model", "service", "outcome", "level")
FIELDS = ("requests", "input_tokens", "output_tokens", "total_tokens", "missing_usage", "inferred_group")
ENDINGS = ("done", "failed", "refused", "hung up", "stopped")
COST_SOURCES = ("recorded", "model", "fallback")


def summary_owner(user, params):
    """Enforce the optional Home scope independently of event-log access."""
    scope = params.get('summary')
    if scope is None:
        return None if user.can('see-log') and params.get('mine') != '1' else user.name
    if scope not in ('mine', 'group', 'total'):
        raise ValueError('Choose your summary, a group or the total.')
    if scope != 'mine' and not user.can('see-summaries'):
        raise PermissionError('Your account may view only your own Home summary.')
    if scope == 'group' and not params.get('group', '').strip():
        raise ValueError('Choose a group.')
    return user.name if scope == 'mine' else None


def empty() -> dict:
    """One total, day or breakdown starts at zero, including genuinely empty days."""
    return {**dict.fromkeys(FIELDS, 0),
            'fallback_input_tokens': 0, 'fallback_output_tokens': 0,
            **{f"{source}_cost": Decimal(0) for source in COST_SOURCES},
            **{f"{source}_requests": 0 for source in COST_SOURCES}}


def amount(value) -> Decimal | None:
    """Accept a known nonnegative numeric cost or rate, including an explicitly free zero."""
    if type(value) not in (int, float):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() and number >= 0 else None


def tally(at, user, model, what, level, detail, bucket: int = HOUR) -> tuple[tuple, list]:
    """One request ending as a usage_hours key and its counts. `bucket` 0 keeps its exact time."""
    try:
        detail = json.loads(detail) if isinstance(detail, str) else detail or {}
    except ValueError:
        detail = {}
    detail = detail if isinstance(detail, dict) else {}
    inputs, outputs = detail.get("prompt_tokens"), detail.get("answer_tokens")
    has_input, has_output = type(inputs) is int and inputs >= 0, type(outputs) is int and outputs >= 0
    inputs, outputs = inputs if has_input else 0, outputs if has_output else 0
    cost, group, service = amount(detail.get("cost")), detail.get("group"), detail.get("service")
    unpriced, complete = cost is None, cost is None and has_input and has_output
    key = (int(at // bucket) * bucket if bucket else at, user, "" if group is None else str(group), model or "(unknown)",
           "" if service is None else str(service), what, level or "normal")
    return key, [1, inputs, outputs, int(not (has_input and has_output)), float(cost or 0),
                 int(unpriced), inputs * unpriced, outputs * unpriced,
                 int(complete), inputs * complete, outputs * complete]


def add_hours(connection, rows) -> None:
    """Count request endings, given as (at, user, model, what, level, detail), into usage_hours."""
    combined = {}
    for at, user, model, what, level, detail in rows:
        if what not in ENDINGS:
            continue
        key, values = tally(at, user, model, what, level, detail)
        combined[key] = [a + b for a, b in zip(combined.get(key, [0] * len(SUMMED)), values)]
    if combined:
        connection.executemany(
            f"INSERT INTO usage_hours ({', '.join(KEY + SUMMED)}) VALUES ({', '.join('?' for _ in KEY + SUMMED)}) "
            f"ON CONFLICT({', '.join(KEY)}) DO UPDATE SET "
            + ", ".join(f"{name} = usage_hours.{name} + excluded.{name}" for name in SUMMED),
            [(*key, *values) for key, values in combined.items()])


def whole_hours(zone, since: float, before: float) -> bool:
    """True when every local midnight and hour in the range falls on a UTC hour, so hourly rows add up exactly."""
    return all(datetime.fromtimestamp(moment, zone).utcoffset().total_seconds() % HOUR == 0 for moment in (since, before - 1))


def has_hours(db) -> bool:
    """Whether this store keeps usage_hours: front's log does once migrated."""
    if getattr(db, "engine", "sqlite") == "sqlite":
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_hours'").fetchone())
    return bool(db.execute("SELECT 1 FROM information_schema.tables WHERE table_name='usage_hours'").fetchone())


def report(db, params, owner: str | None = None, groups: dict[str, str] | None = None,
           models: dict[str, dict] | None = None) -> dict:
    """Aggregate authorized endings once; inclusive date filters use local midnight boundaries."""
    rates = {}
    for side, default in (("input", "1"), ("output", "3")):
        try:
            rate = Decimal(str(params.get(f"{side}_rate", default)))
            if not rate.is_finite() or not 0 <= rate <= 1_000_000:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Mock rates must be numbers from 0 to 1,000,000 USD per million tokens.") from None
        rates[side] = rate
    timezone = params.get("timezone") or "UTC"
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Choose a valid timezone, such as America/Vancouver.") from None
    placeholders = ",".join("?" for _ in ENDINGS)
    scope = " AND e.user = ?" if owner is not None else ""
    scope_values = (owner,) if owner is not None else ()
    hours = has_hours(db)
    oldest = db.execute(
        f"SELECT MIN(hour) FROM usage_hours WHERE 1 = 1{scope.replace('e.user', 'person')}" if hours else
        f"SELECT MIN(e.at) FROM events e WHERE e.kind = 'request' AND e.what IN ({placeholders}){scope}",
        scope_values if hours else (*ENDINGS, *scope_values),
    ).fetchone()[0]
    period = params.get('period')
    if period is not None and period not in ('1', '7', '30', 'all'):
        raise ValueError('Choose 1 day, 7 days, 30 days or all time.')
    try:
        end = date.fromisoformat(params.get("end") or datetime.now(zone).date().isoformat())
        if period == 'all':
            start = min(end, datetime.fromtimestamp(oldest, zone).date()) if oldest is not None else end
        elif period:
            start = end - timedelta(days=int(period) - 1)
        else:
            start = date.fromisoformat(params.get("start") or (end - timedelta(days=29)).isoformat())
        until = end + timedelta(days=1)
    except (ValueError, OverflowError):
        raise ValueError("Start and end must be valid dates in YYYY-MM-DD form.") from None
    days = (end - start).days + 1
    if days < 1 or (period != 'all' and days > 3660):
        raise ValueError("Choose an end on or after the start, spanning at most 3,660 days.")
    bucket = params.get("bucket") or "day"
    if bucket not in ("day", "hour") or (bucket == "hour" and days != 1):
        raise ValueError("Choose daily totals, or hourly totals for one day.")
    group_by = params.get("group_by") or "model"
    if group_by not in DIMENSIONS:
        raise ValueError("Group by user, group, model, source or outcome.")
    filters = {key: params.get(key, "").strip() for key in DIMENSIONS}
    if owner is not None:
        if filters["user"] and filters["user"] != owner:
            raise PermissionError("You may view only your own usage.")
        filters["user"] = owner
    since = datetime.combine(start, time.min, zone).timestamp()
    before = datetime.combine(until, time.min, zone).timestamp()
    if hours and whole_hours(zone, since, before):
        records = db.execute(
            f"SELECT {', '.join(KEY + SUMMED)} FROM usage_hours WHERE hour >= ? AND hour < ?{scope.replace('e.user', 'person')}",
            (since, before, *scope_values)).fetchall()
    else:
        records = [(*key, *values) for key, values in (tally(*row, bucket=0) for row in db.execute(
            f"""SELECT e.at, e.user, e.model, e.what, e.level, e.detail FROM events e
                WHERE e.kind = 'request' AND e.what IN ({placeholders}) AND e.at >= ? AND e.at < ?{scope}""",
            (*ENDINGS, since, before, *scope_values)))]
    daily = {(start + timedelta(days=i)).isoformat(): empty() for i in range(days)}
    # Build hours from absolute instants between local midnights. A daylight-saving day
    # therefore has the correct 23 or 25 points instead of inventing or merging an hour.
    hourly = ({index: empty() for index in range(round((before - since) / 3600))}
              if bucket == "hour" else None)
    choices = {key: set() for key in DIMENSIONS}
    user_groups = {name: {group} for name, group in (groups or {}).items() if owner is None or name == owner}
    totals = empty()
    breakdowns = {key: {} for key in DIMENSIONS}
    user_models = {}
    for at, user, group, model, service, outcome, level, *summed in records:
        counted = dict(zip(SUMMED, summed))
        inferred = group == ""
        group = group if not inferred else (groups or {}).get(user, "(unknown)")
        source = "hosted" if service else "local"
        identity = dict(zip(DIMENSIONS, (user, group, model, source, outcome, level)))
        user_groups.setdefault(user, set()).add(group)
        for key, value in identity.items():
            choices[key].add(value)
        if any(wanted and identity[key] != wanted for key, wanted in filters.items()):
            continue
        requests, inputs, outputs = counted["requests"], counted["input_tokens"], counted["output_tokens"]
        values = dict(zip(FIELDS, (requests, inputs, outputs, inputs + outputs, counted["missing_usage"],
                                   requests if inferred else 0)))
        values.update(recorded_cost=Decimal(str(counted["recorded_cost"])),
                      recorded_requests=requests - counted["unpriced_requests"])
        unpriced = [counted["unpriced_requests"], counted["unpriced_input"], counted["unpriced_output"]]
        price = (models or {}).get(model, {})
        complete = counted["complete_requests"], counted["complete_input"], counted["complete_output"]
        if service and price.get("service") == service and complete[0]:
            input_rate, output_rate = amount(price.get("in")), amount(price.get("out"))
            # A missing rate is only harmless when that side used no tokens. Counted together, one
            # ending with tokens on that side sends the whole row to the fallback rate.
            if ((input_rate is not None or output_rate is not None)
                    and (input_rate is not None or complete[1] == 0)
                    and (output_rate is not None or complete[2] == 0)):
                values.update(model_cost=(complete[1] * (input_rate or 0) + complete[2] * (output_rate or 0)) / Decimal(1_000_000),
                              model_requests=complete[0])
                unpriced = [have - priced for have, priced in zip(unpriced, complete)]
        values.update(fallback_cost=(unpriced[1] * rates["input"] + unpriced[2] * rates["output"]) / 1_000_000,
                      fallback_requests=unpriced[0], fallback_input_tokens=unpriced[1], fallback_output_tokens=unpriced[2])
        day = datetime.fromtimestamp(at, zone).date().isoformat()
        grouped = [breakdowns[key].setdefault(identity[key], empty()) for key in DIMENSIONS]
        model_total = user_models.setdefault((user, model), empty())
        targets = [totals, daily[day], *grouped, model_total]
        if hourly is not None:
            targets.insert(2, hourly[int((at - since) // 3600)])
        for target in targets:
            for key, value in values.items():
                target[key] += value
    # Price aggregate counts, without rounding individual requests or changing recorded costs.
    aggregates = [values for grouped in breakdowns.values() for values in grouped.values()]
    for values in (totals, *daily.values(), *(hourly or {}).values(), *aggregates, *user_models.values()):
        costs = {side: Decimal(values[f"{side}_tokens"]) * rate / 1_000_000
                 for side, rate in rates.items()}
        values.update({f"{side}_cost": float(cost) for side, cost in costs.items()})
        values["total_cost"] = float(sum(costs.values()))
        values["effective_cost"] = float(sum(values[f"{source}_cost"] for source in COST_SOURCES))
        for source in COST_SOURCES:
            values[f"{source}_cost"] = float(values[f"{source}_cost"])
    grouped_rows = {key: [{"name": name, **values} for name, values in
                         sorted(grouped.items(), key=lambda item: (-item[1]["total_tokens"], item[0]))]
                    for key, grouped in breakdowns.items()}
    choices['user'].update(user_groups)
    choices['group'].update(group for memberships in user_groups.values() for group in memberships)
    if filters['group']:
        choices['user'] = {name for name, memberships in user_groups.items() if filters['group'] in memberships}
    hourly_rows = []
    if hourly is not None:
        moments = [datetime.fromtimestamp(since + index * 3600, zone) for index in hourly]
        short_labels = [moment.strftime("%H:%M") for moment in moments]
        repeated = {label for label in short_labels if short_labels.count(label) > 1}
        hourly_rows = [{"at": moment.isoformat(timespec="minutes"),
                        "label": moment.strftime("%H:%M %Z") if label in repeated else label,
                        **hourly[index]}
                       for index, (moment, label) in enumerate(zip(moments, short_labels))]
    return {
        "start": start.isoformat(), "end": end.isoformat(), "timezone": timezone,
        "bucket": bucket, "hourly": hourly_rows,
        "group_by": group_by, "filters": filters,
        "mock_pricing": {"currency": "USD", "per_tokens": 1_000_000,
                         **{f"{side}_rate": float(rate) for side, rate in rates.items()}},
        "choices": {key: sorted(values) for key, values in choices.items()},
        "user_groups": {name: sorted(memberships) for name, memberships in user_groups.items()},
        "totals": totals,
        "daily": [{"date": day, **values} for day, values in daily.items()],
        "breakdown": grouped_rows[group_by],
        "breakdowns": grouped_rows,
        "user_models": [{"user": user, "model": model, **values}
                        for (user, model), values in sorted(user_models.items())],
        "oldest": datetime.fromtimestamp(oldest, zone).date().isoformat() if oldest is not None else None,
    }
