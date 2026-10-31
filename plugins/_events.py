"""Read adapter extensions for openWeightGym chat without changing compute's capture protocol."""

import json


def append(into: list, events: list) -> None:
    """Keep every available event while joining consecutive token previews for readable display."""
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "preview" and into and into[-1].get("type") == "preview":
            into[-1]["text"] += event.get("text") or ""
        else:
            into.append(dict(event))


def streamed(raw: bytes, into: list) -> None:
    """Append extension events from complete SSE lines, leaving partial UTF-8 lines pending."""
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                for choice in json.loads(line[5:]).get("choices") or []:
                    append(into, (choice.get("delta") or {}).get("plugin_events") or [])
            except (ValueError, AttributeError, TypeError):
                continue


def completed(raw: bytes, content_type: str) -> list:
    """Recover available progress from a saved completion or an assembled event stream."""
    result = []
    if "text/event-stream" in content_type:
        streamed(raw, result)
    else:
        try:
            append(result, json.loads(raw)["choices"][0]["message"].get("plugin_events") or [])
        except (ValueError, KeyError, IndexError, AttributeError, TypeError):
            pass
    return result
