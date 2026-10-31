"""What is kept of a sensitive request: the request, the answer, and the tool calls in between.

Nothing here touches the database or the web. These functions turn a request body and an answer
into the pieces the event log stores, and back into something readable.

A harness sends the whole conversation again with every call. To avoid keeping it many times
over, each message is stored the first time it is seen in a piece of work, and afterwards only
referred to by its hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable


def text_hash(thing) -> str:
    """A short fingerprint of a message or a list of tools, the same for the same content."""
    return hashlib.sha256(json.dumps(thing, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def request_text(asked: dict, seen: Callable[[str], bool]) -> tuple[dict, list[dict], list[str]]:
    """Split a request body into what to keep.

    `seen(hash)` says whether a piece of text was captured earlier in the same piece of work.

    Returns three things: the text for the request's own log entry, the tool results that arrived
    with this request and have not been kept before (each gets an entry of its own), and the
    hashes of everything new, to be remembered as seen.
    """
    new, results = [], []

    def keep(thing, key: str) -> dict:
        """The thing itself if it is new, or a reference to where it was kept before."""
        fingerprint = text_hash(thing)
        if seen(fingerprint) or fingerprint in new:
            return {"seen": fingerprint}
        new.append(fingerprint)
        return {"hash": fingerprint, key: thing}

    messages = []
    for message in asked.get("messages") or []:
        kept = keep(message, "message")
        if "message" in kept and isinstance(message, dict) and message.get("role") == "tool":
            results.append(kept)  # a tool's result, reported back by the harness: its own entry
            kept = {"seen": kept["hash"]}
        messages.append(kept)

    text = {"body": {key: value for key, value in asked.items() if key not in ("messages", "tools")}, "messages": messages}
    if asked.get("tools"):
        text["tools"] = keep(asked["tools"], "tools")
    return text, results, new


def said_wrong(raw: bytes) -> str | None:
    """The error an answer carries inside itself, in the service's own words, or None if it carries none.

    An outside service may answer 200 and then say, in the body, that it could not do it: the
    model's provider failed, or too much was asked of a free model. In a streamed answer that
    is one piece among the others, with `error` in the place of `choices`.
    """
    for line in raw.decode(errors="replace").splitlines():
        if '"error"' not in line:
            continue
        try:
            error = json.loads(line.removeprefix("data:")).get("error")
        except (ValueError, AttributeError):
            continue
        if isinstance(error, dict) and error.get("message"):
            return f"{error['message']}" + (f" (code {error['code']})" if error.get("code") else "")
        if isinstance(error, str) and error:
            return error
    return None


def answer_text(raw: bytes, content_type: str) -> tuple[dict, list[dict]]:
    """Read an answer into what to keep: the answer itself, and the tool calls the model asked for.

    A streamed answer arrives as hundreds of small pieces. They are put back together here, so
    that what is kept is the answer and not the wrapping around every token.
    """
    answer = {"content": "", "reasoning": "", "finish_reason": None}
    calls: dict[int, dict] = {}

    def take(message: dict, finish_reason) -> None:
        """Merge one assistant message delta, finish reason and tool-call fragments."""
        answer["content"] += message.get("content") or ""
        answer["reasoning"] += thinking_of(message)
        answer["finish_reason"] = finish_reason or answer["finish_reason"]
        for position, piece in enumerate(message.get("tool_calls") or []):
            call = calls.setdefault(piece.get("index", position), {"id": "", "tool": "", "arguments": ""})
            function = piece.get("function") or {}
            call["id"] += piece.get("id") or ""
            call["tool"] += function.get("name") or ""
            call["arguments"] += function.get("arguments") or ""

    try:
        if "text/event-stream" in content_type:
            for line in raw.decode(errors="replace").splitlines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    for choice in json.loads(line[5:]).get("choices") or []:
                        take(choice.get("delta") or {}, choice.get("finish_reason"))
        else:
            choice = json.loads(raw)["choices"][0]
            take(choice["message"], choice.get("finish_reason"))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Not a chat answer, or not in a shape that is understood. Keep it as it came.
        return {"raw": raw.decode(errors="replace")}, []

    asked_for = []
    for call in calls.values():
        try:
            call["arguments"] = json.loads(call["arguments"])  # readable as named parameters
        except ValueError:
            pass  # the model wrote something that is not JSON; keep its exact text
        asked_for.append(call)
    answer["tool_calls"] = [{"id": call["id"], "tool": call["tool"]} for call in asked_for]
    answer["error"] = said_wrong(raw)
    kept = {key: value for key, value in answer.items() if value not in ("", None, [])}
    # Nothing in it was understood: no words, no thinking, no tool call, no error. Then its beginning is kept as it came, so that there is something to look at.
    return kept or {"raw": raw.decode(errors="replace")[:4000]}, asked_for


def thinking_of(message: dict) -> str:
    """A model's thinking in one piece of an answer. llama-server calls it `reasoning_content`, and OpenRouter `reasoning`."""
    thought = message.get("reasoning_content") or message.get("reasoning") or ""
    return thought if isinstance(thought, str) else ""


def stream_text(lines: bytes, into: dict) -> None:
    """Add what some whole lines of a streamed answer say to `into`, which holds the answer and the thinking so far.

    This is for showing an answer while it is written: each time, only the lines that are new
    are read. `answer_text` reads a finished answer in one go, tool calls included.
    """
    for line in lines.decode(errors="replace").splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                choices = json.loads(line[5:]).get("choices") or []
            except (ValueError, AttributeError):
                continue
            for choice in choices:
                delta = choice.get("delta") or {}
                into["content"] += delta.get("content") or ""
                into["reasoning"] += thinking_of(delta)


def resolve(entries: list[dict]) -> list[dict]:
    """Fill in references to text kept earlier, wherever the earlier entry is among `entries`.

    Reading a whole piece of work this way gives back each request as it was sent. A reference
    whose text is not among the entries is left as a reference.
    """
    kept: dict[str, dict] = {}

    def remember(piece) -> None:
        """Index a captured text fragment by its content fingerprint."""
        if isinstance(piece, dict) and "hash" in piece:
            kept[piece["hash"]] = piece

    def fill(piece):
        """Resolve a captured-text reference when its original fragment is available."""
        return kept.get(piece["seen"], piece) if isinstance(piece, dict) and "seen" in piece else piece

    for entry in entries:
        text = entry.get("text")
        if not text:
            continue
        remember(text)  # a tool result's entry is one kept message
        if "messages" in text:
            text["messages"] = [fill(message) for message in text["messages"]]
            for message in text["messages"]:
                remember(message)
        if "tools" in text:
            text["tools"] = fill(text["tools"])
            remember(text["tools"])
    return entries
