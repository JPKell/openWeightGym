"""Private chat and collection orchestration hosted inside WeightRoom with local storage.

Compute is reached only through authenticated HTTP APIs. Browser disconnection does not
cancel a turn; stopping WeightRoom does, retaining partial output without automatic replay.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from db import DatabaseSource
from weightroom.chat_store import (SUMMARY_PROMPT, TOO_LONG, Conversations, attachments,
    check_sampling, message_body, readable, tokens, trace_of, with_memory)
from loadcoach.capture import answer_text, stream_text
from loadcoach.roles import STRENGTHS
from loadcoach.users import keep_private
from plugins._events import completed as plugin_events, streamed as stream_plugin_events
from weightroom.knowledge import Knowledge
from weightroom.workflows import Workflow


@dataclass
class Principal:
    """The caller's immutable account identity and current chat permissions from compute."""

    name: str
    info: dict

    def can(self, ability: str) -> bool:
        """Check an effective permission supplied by compute, including app restrictions."""
        return ability in self.info.get("can", [])

    def options(self, model: str) -> dict:
        """Return public chat capabilities, never backend paths or credentials."""
        return self.info.get("chat", {}).get("models", {}).get(model, {})

    def current_name(self, identity: str) -> str | None:
        """Resolve only currently permitted models, independently of a recorded label."""
        return next((name for name,options in self.info.get('chat',{}).get('models',{}).items() if options.get('model_id')==identity),None)


@dataclass
class Flight:
    """The streamed response collected by WeightRoom, independent of browser connections."""

    whole: bytearray = field(default_factory=bytearray)
    entry: int = 0
    headers: dict = field(default_factory=dict)


@dataclass
class Turn:
    """One branch of a web-chat answer; its task lives only as long as WeightRoom."""

    task: asyncio.Task | None = None
    flight: Flight | None = None
    state: str = ""
    stopped: bool = False
    done: bool = False
    model: str = ""
    owner: str = ""
    hosted: bool = False
    adaptor: bool = False
    answer: dict | None = None


def error(status: int, message: str) -> JSONResponse:
    """Return a chat failure using the existing error envelope."""
    return JSONResponse({"error": {"message": message, "type": "chat_error"}}, status_code=status)


def reason(body: bytes) -> dict:
    """Extract the upstream error without retaining an unbounded response."""
    try:
        why = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        why = body.decode(errors="replace")
    return {"why": str(why)[:500]}


def build_app(database: DatabaseSource, compute: httpx.AsyncClient, inference: httpx.AsyncClient | None = None, *, plugins=None) -> Starlette:
    """Host web-chat APIs in WeightRoom's process using the user's own upstream key."""
    inference = inference or compute
    chats = Conversations(database)
    knowledge = Knowledge(database, compute)
    workflows = Workflow(database, compute, knowledge)
    keep_private(database)
    passing = Conversations(":memory:")
    turns: dict[str, list[Turn]] = {}

    def offered(user: Principal) -> list[str]:
        """List concrete models this caller may ask for."""
        options = user.info.get("chat", {}).get("models", {})
        return [name for name in user.info.get("models", []) if not options.get(name, {}).get("image_generation") and not options.get(name, {}).get("audio")]

    def role_of(user: Principal, model: str) -> bool:
        """Recognize a permitted role and its optional routing strength."""
        name, _, strength = model.partition(":")
        return model not in offered(user) and name in user.info.get("roles", []) and (not strength or strength in STRENGTHS)

    def no_such_model(model: str, user: Principal) -> Response:
        """Hide forbidden models using the same response as an unknown name."""
        return error(404, f"The model {model!r} does not exist. Available: {', '.join(offered(user))}")

    async def read_json(request: Request, allowed: set[str]) -> dict | Response:
        """Reject malformed bodies and fields outside this chat operation."""
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 24 * 1024 * 1024:
                    return error(413, "The message and attachments are too large.")
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict) or body.keys() - allowed:
                raise ValueError()
            return body
        except (ValueError, TypeError):
            return error(400, "Expected an object with fields: " + ", ".join(sorted(allowed)))

    async def request_ending(entry: int, headers: dict) -> dict:
        """Read usage from compute's ordinary request log after the streamed response ends."""
        try:
            response = await compute.get(f"/loadcoach/requests/{entry}", headers=headers)
            return (response.json().get("ended") or {}) if response.is_success else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def fetch_answer(turn: Turn, body: dict, headers: dict, store: Conversations, number: int, session_key: str | None) -> httpx.Response:
        """Collect an actual HTTP stream and expose growing output to reconnecting browsers."""
        turn.flight = Flight()
        async with inference.stream("POST", "/v1/chat/completions", json=body, headers=headers, timeout=None) as response:
            turn.flight.entry = int(response.headers.get("x-loadcoach-request-id", 0))
            turn.flight.headers = dict(response.headers)
            if session_key and response.headers.get("x-suite-session-id"):
                store.session(number, session_key, response.headers["x-suite-session-id"])
            async for piece in response.aiter_bytes():
                turn.flight.whole.extend(piece)
        return httpx.Response(response.status_code, headers=response.headers, content=bytes(turn.flight.whole))

    def talk_of(request: Request, user: Principal) -> tuple[str, Conversations, int, dict] | Response:
        """The conversation the path names: its id, where it is kept, its number there, and what was said. Only its owner has it."""
        ref = request.path_params["ref"]
        store, number = (passing, ref[1:]) if ref.startswith("t") else (chats, ref)
        if number.isdigit():
            store.bind_models(int(number),user.name,user.info.get('chat',{}).get('models',{}))
        talk = store.get(int(number), user.name) if number.isdigit() else None
        if talk is None:
            return error(404, "You have no such conversation.")  # someone else's is answered like one that is not there
        return ref, store, int(number), current_labels(talk,user)

    def current_labels(talk: dict, user: Principal) -> dict:
        """Present current labels for pinned replies, retaining their original recorded names."""
        compare=list(talk.get('compare') or [])
        missing=[]
        for message in talk['messages']:
            identity=message.get('model_id')
            if not identity:
                continue
            name=user.current_name(identity)
            if name:
                message['model_at_reply']=message.get('model')
                message['model']=name
                if compare:
                    compare[message.get('branch',0)]=name
            elif compare:
                missing.append(identity)
        return {**talk,'compare':compare,'unavailable_model_ids':list(dict.fromkeys(missing))}

    def shown(ref: str, talk: dict) -> dict:
        """Show one conversation, with shared questions and independently ordered comparison replies."""
        group = turns.get(ref)
        pending = {"state": group[0].state or ("answering" if group[0].flight else "starting")} if group else None
        exchanges = []
        if talk.get("compare"):
            for message in talk["messages"]:
                if message["role"] == "user":
                    exchanges.append({"question": message, "answers": [None, None]})
                elif exchanges:
                    exchanges[-1]["answers"][message.get("branch", 0)] = message
        return {**talk, "id": ref, "temporary": ref.startswith("t"), "pending": pending, "exchanges": exchanges}

    def begin(ref: str, store: Conversations, number: int, request: Request, user: Principal, model: str, carry_on: bool = False) -> None:
        """Start one answer per chosen model; each has its own request, history and cancellation."""
        models = current_labels(store.get(number,user.name),user).get('compare') or [model]
        turns[ref] = [Turn(model=name, owner=user.name) for name in models]
        for branch, turn in enumerate(turns[ref]):
            if not carry_on:  # more of an answer that is there already loses nothing if it is cut off
                store.answering(number, branch, user.name, turn.model)
            turn.hosted = user.options(turn.model).get("hosted", False)
            turn.adaptor = user.options(turn.model).get("adaptor", False)
            turn.task = asyncio.create_task(next_answer(ref, store, number, user.name, request.headers.get("authorization", ""), turn.model, carry_on, branch, turn, user))

    async def compact(turn: Turn, said: list[dict], memory: dict, model: str, context: int, headers: dict, force: bool = False) -> dict:
        """Summarize an older prefix in bounded requests through the caller's normal model API."""
        covered = max(0, len(said) - (1 if force else 3))
        # The prefix ends after an assistant reply, never in the middle of an exchange.
        while covered and said[covered - 1]["role"] != "assistant":
            covered -= 1
        start = memory.get("covered", 0)
        if covered <= start:
            return memory
        if context < 512:
            raise ValueError("This model's context is too small for conversation summaries. Choose a larger context.")
        turn.state = "Summarizing older messages"
        text = "\n".join(m["role"] + ": " + readable(m["content"]) for m in said[start:covered])
        summary = memory.get("text", "")
        output = min(512, context // 8)
        for _ in range(64):
            if turn.stopped:
                raise ValueError("Stopped while summarizing. The original messages are unchanged.")
            prefix = "Previous summary:\n" + summary + "\nOlder messages:\n"
            room = int(context * .65) - tokens([{"content": SUMMARY_PROMPT}, {"content": prefix}])
            if room < 32:
                raise ValueError("The summary cannot fit this model's context. Choose a larger context.")
            # Each UTF-8 byte is counted as a token here, leaving space even for unusual text.
            chunk = text.encode()[:room].decode(errors="ignore")
            if not chunk:
                break
            body = {"model": model, "stream": True, "max_tokens": output,
                    "messages": [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": prefix + chunk}]}
            if not turn.hosted:
                body["chat_template_kwargs"] = {"enable_thinking": False}
            answer = await inference.post("/v1/chat/completions", json=body, headers=headers, timeout=None)
            parsed, _ = answer_text(answer.content, answer.headers.get("content-type", ""))
            summary = parsed.get("content", "").strip()
            if turn.stopped or answer.status_code != 200 or not summary or parsed.get("finish_reason") != "stop":
                raise ValueError("Could not finish the context summary. Original messages are unchanged; retry or choose a larger model context.")
            text = text[len(chunk):]
            if not text:
                turn.flight, turn.state = None, ""
                return {"covered": covered, "text": summary, "model": model}
        raise ValueError("Too much older text to summarize in one turn. Choose a larger model context.")

    async def next_answer(ref: str, store: Conversations, number: int, user: str, signed: str, model: str, carry_on: bool, branch: int, turn: Turn, principal: Principal) -> None:
        """Fetch a conversation's next answer from a model, and keep it.

        It runs to its end whoever is watching. WeightRoom shutdown interrupts the turn.
        The model is asked through HTTP with the user's own key, using ordinary compute
        permissions, queueing, request logging and usage accounting. `carry_on` asks for more of an answer that was cut off.
        """
        # The request below runs in this same task. That is how `forward` finds this turn, to hand its request back.
        # The request runs in this WeightRoom process; no compute-local task state is used.
        headers = {"authorization": signed}
        # The trace follows the existing chat IDs, preserving links to historical compute logs.
        headers |= {"x-loadcoach-log": "off"} if store is passing else {"x-loadcoach-trace": trace_of(number)}
        try:
            before = None
            talk = store.get(number, user) or {"messages": [], "system": "", "thinking": True, "sampling": {}}
            said = [m for m in talk["messages"] if m["role"] == "user" or m.get("branch", 0) == branch]
            before = said[-1] if carry_on else None
            memory = talk.get("memory", {})
            if talk.get("compare"):
                memory = memory.get(str(branch), {})
            options = principal.options(model)
            context = options.get("context") or 8192
            session_key = None
            if options.get("adaptor") and plugins:
                target = plugins.resolve(model)
                if target:
                    session_key = json.dumps(list(target[:3]))
                headers["X-Suite-Session-Mode"] = talk.get("session_mode", "fresh")
                if headers["X-Suite-Session-Mode"] == "persistent" and session_key and talk.get("sessions", {}).get(session_key):
                    headers["X-Suite-Session-ID"] = talk["sessions"][session_key]
            messages = [message_body(m) for m in said]
            if options.get("adaptor") and carry_on:
                messages.append({"role": "user", "content": "Continue the interrupted reply. Do not repeat completed tool actions."})
            for attempt in range(3):
                sent = with_memory(messages, memory)
                if talk["system"]:
                    sent.insert(0, {"role": "system", "content": talk["system"]})
                if not options.get("adaptor") and (attempt or tokens(sent) >= context * .75):
                    memory = await compact(turn, messages, memory, model, context, headers, force=bool(attempt))
                    kept = (store.get(number, user) or {}).get("memory", {})
                    store.remember(number, {**kept, str(branch): memory} if talk.get("compare") else memory)
                    sent = with_memory(messages, memory)
                    if talk["system"]:
                        sent.insert(0, {"role": "system", "content": talk["system"]})
                if turn.stopped:
                    raise ValueError("Stopped before answering.")
                body = {"model": model, "messages": sent, "stream": True, **talk["sampling"]}  # with how the model is to pick its words, where the conversation says
                if not options.get("adaptor") and not principal.options(model).get("hosted") and (carry_on or not talk["thinking"]):
                    # A model whose template has a thinking switch is told not to think. One without the switch takes no notice.
                    # An answer that is carried on cannot begin with thinking, so then it is always off.
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                answer = await fetch_answer(turn, body, headers, store, number, session_key)
                try:
                    refusal = json.loads(answer.content).get("error") or {} if answer.status_code == 400 else {}
                except (ValueError, AttributeError):
                    refusal = {}
                if not isinstance(refusal, dict) or refusal.get("type") != TOO_LONG:
                    break
                context = int(refusal.get("n_ctx") or context)
            text, _ = answer_text(answer.content, answer.headers.get("content-type", ""))
            entry = int(answer.headers.get("x-loadcoach-request-id", 0))
            # How it went, from the log: the figures shown under the answer.
            ending = await request_ending(entry, headers) if entry else {}
            detail = {key: ending.get(key) for key in ("prompt_tokens", "answer_tokens", "tokens_per_second", "waited", "ran")}
            # Asked for under a role, the answer is the picked model's. That is the model the conversation then keeps: its next turn names it.
            picked = answer.headers.get("x-loadcoach-picked")
            detail |= {"asked_as": model, "because": answer.headers.get("x-loadcoach-because")} if picked else {}
            detail |= {"model": picked or model, "request": entry, "summarized": memory.get("covered", 0), "reasoning": text.get("reasoning"), "finish": text.get("finish_reason")}
            if identity := answer.headers.get('x-loadcoach-model-id') or principal.options(picked or model).get('model_id'):
                detail['model_id']=identity
            if answer.headers.get("x-suite-adaptor"):
                detail["adaptor"] = {key: answer.headers.get("x-suite-" + header) for key, header in
                    (("plugin", "adaptor"), ("instance", "instance"), ("profile", "profile"), ("session_id", "session-id"), ("request_id", "request-id"))}
                detail["plugin_events"] = plugin_events(answer.content, answer.headers.get("content-type", ""))
                if session_key and answer.headers.get("x-suite-session-id"):
                    store.session(number, session_key, answer.headers["x-suite-session-id"])
            if answer.status_code != 200:
                detail["error"] = reason(answer.content).get("why") or f"The model server answered {answer.status_code}."
            elif text.get("error") or not (text.get("content") or text.get("reasoning") or text.get("tool_calls")):
                # It said 200 and gave nothing to show. The conversation says so, with the service's own words if it gave any, and is not left with a blank.
                detail["error"] = text.get("error") or "The model answered with nothing. What came back is on the request's page."
            content = text.get("content", "")
            if before:  # more of the same answer: the two parts become one
                content, detail["reasoning"] = before["content"] + content, before.get("reasoning")
                store.drop_answer(number)
            store.say(number, "assistant", content, branch=branch, **detail)
            store.answered(number, branch)
            turn.answer = {"content": content, **detail}
        except asyncio.CancelledError:
            text, _ = answer_text(bytes(turn.flight.whole), "text/event-stream") if turn.flight else ({}, [])
            content = text.get("content", "")
            if carry_on:
                content = (before or {}).get("content", "") + content
                store.drop_answer(number)
            turn.answer = {"content": content, "model": model, "reasoning": text.get("reasoning"),
                           "request": turn.flight.entry if turn.flight else 0}
            if identity := (turn.flight.headers.get('x-loadcoach-model-id') if turn.flight else None) or principal.options(model).get('model_id'):
                turn.answer['model_id'] = identity
            if turn.adaptor and turn.flight:
                turn.answer["plugin_events"] = plugin_events(bytes(turn.flight.whole), "text/event-stream")
                turn.answer["adaptor"] = {key: turn.flight.headers.get("x-suite-" + header) for key, header in
                    (("plugin", "adaptor"), ("instance", "instance"), ("profile", "profile"), ("session_id", "session-id"), ("request_id", "request-id"))}
            if not turn.stopped:
                turn.answer["error"] = "WeightRoom stopped during this answer. Ask again to retry."
            store.say(number, "assistant", branch=branch, **turn.answer)
            store.answered(number, branch)
        except Exception as problem:
            partial, _ = answer_text(bytes(turn.flight.whole), "text/event-stream") if turn.flight else ({}, [])
            content = partial.get("content", "")
            if carry_on:
                content = (before or {}).get("content", "") + content
                store.drop_answer(number)
            turn.answer = {"content": content, "model": model, "reasoning": partial.get("reasoning"),
                           "error": f"{type(problem).__name__}: {problem}"}
            if identity := (turn.flight.headers.get('x-loadcoach-model-id') if turn.flight else None) or principal.options(model).get('model_id'):
                turn.answer['model_id'] = identity
            if turn.adaptor and turn.flight:
                turn.answer["plugin_events"] = plugin_events(bytes(turn.flight.whole), "text/event-stream")
            store.say(number, "assistant", branch=branch, **turn.answer)
            store.answered(number, branch)
        finally:
            turn.state = ""
            turn.done = True
            if all(t.done for t in turns.get(ref, [])):
                turns.pop(ref, None)

    async def conversation_list(request: Request, user: Principal) -> Response:
        """One's own conversations, the one last spoken in first."""
        mine = [{**talk, "id": f"t{talk['id']}", "temporary": True} for talk in passing.listing(user.name)] + [{**talk, "id": str(talk["id"])} for talk in chats.listing(user.name)]
        return JSONResponse({"conversations": sorted(mine, key=lambda talk: talk["updated"], reverse=True)})

    async def conversation_start(request: Request, user: Principal) -> Response:
        """Start a conversation with a first message, and set the answer going.

        The body gives `content` and `model`, and may give `system`, instructions that go
        before everything said, `thinking`, false to ask the model not to think first,
        `sampling`, which may set temperature, top_k, top_p and min_p for every answer, and
        `temporary`, true for a conversation that is not kept and whose requests are unlogged.
        """
        asked = await read_json(request, {"content", "model", "system", "thinking", "sampling", "temporary", "attachments", "compare_model", "session_mode"})
        if isinstance(asked, Response):
            return asked
        content, model, system = asked.get("content"), asked.get("model"), asked.get("system", "")
        if asked.get("session_mode", "fresh") not in ("fresh", "persistent"):
            return error(400, "session_mode is fresh or persistent.")
        if not isinstance(content, str) or not content.strip() or not isinstance(system, str) or not isinstance(asked.get("thinking", True), bool):
            return error(400, "content is what you say, system is text, and thinking is true or false.")
        try:
            check_sampling(asked.get("sampling", {}))
            files = attachments(asked.get("attachments", []))
        except ValueError as problem:
            return error(400, str(problem))
        if not isinstance(model, str) or (model not in offered(user) and not role_of(user, model)):
            return no_such_model(str(model), user)
        # A conversation may be started under a role. The role finds a model that sees among its own, or says that it has none.
        if any(f["mime"].startswith("image/") for f in files) and not role_of(user, model) and not user.options(model).get("vision"):
            return error(400, "Images need a local vision model configured with an mmproj projector. Choose one or remove the images.")
        compare = asked.get("compare_model")
        if compare:
            if not isinstance(compare, str) or compare not in offered(user):
                return no_such_model(str(compare), user)
            if compare == model:
                return error(400, "Choose two different models to compare.")
            if any(f["mime"].startswith("image/") for f in files) and not user.options(compare).get("vision"):
                return error(400, "Both comparison models must support images with an mmproj projector.")
        store = chats
        if asked.get("temporary"):
            if not user.info.get("chat", {}).get("unlogged", False):
                return error(403, "Temporary conversations are switched off here: unlogged is false in LoadCoach's settings.")
            if not user.can("unlogged"):
                return error(403, "You may not send unlogged requests.")
            store = passing
        number = store.start(user.name, content.strip().splitlines()[0][:60], system, asked.get("thinking", True), asked.get("sampling"), [model, compare] if compare else [])
        store.session_mode(number, asked.get("session_mode", "fresh"))
        store.say(number, "user", content, attachments=files)
        ref = f"t{number}" if store is passing else str(number)
        begin(ref, store, number, request, user, model)
        return JSONResponse({"id": ref}, status_code=201)

    async def conversation_spoken(request: Request, user: Principal) -> Response:
        """Keep one exchange of a spoken call that already happened: what was heard and what was said back.

        No model is asked. The first exchange starts a conversation, whose id the caller sends with
        the next ones. `audio` may name SoundRoom recordings of the turn, kept with the messages.
        """
        asked = await read_json(request, {"conversation", "said", "answered", "model", "voice", "audio"})
        if isinstance(asked, Response):
            return asked
        said, answered, model = asked.get("said"), asked.get("answered", ""), asked.get("model", "")
        if not isinstance(said, str) or not said.strip() or len(said) > 8000 or not isinstance(answered, str) or len(answered) > 32000 or not isinstance(model, str):
            return error(400, "said is what the caller said, answered is what was spoken back, and model names who answered.")
        audio = asked.get("audio", [])
        if not isinstance(audio, list) or len(audio) > 2 or not all(isinstance(one, str) and len(one) <= 80 for one in audio):
            return error(400, "audio is a list of at most two recording ids.")
        ref = asked.get("conversation")
        if ref is None:
            number = chats.start(user.name, "Spoken: " + said.strip()[:52], "", True, None, [])
        elif isinstance(ref, str) and ref.isdigit() and chats.get(int(ref), user.name) is not None:
            number = int(ref)
        else:
            return error(404, "There is no such conversation of yours.")
        chats.say(number, "user", said.strip(), spoken=True, audio=audio[:1])
        chats.say(number, "assistant", answered.strip(), model=model, spoken=True, voice=asked.get("voice"), audio=audio[1:])
        return JSONResponse({"id": str(number)}, status_code=201)

    async def conversation_one(request: Request, user: Principal) -> Response:
        """Return an owner-scoped conversation with current model labels and live state."""
        found = talk_of(request, user)
        return found if isinstance(found, Response) else JSONResponse(shown(found[0], found[3]))

    async def conversation_change(request: Request, user: Principal) -> Response:
        """Change a conversation's title, its instructions, whether the model is asked to think, or how it is to pick its words."""
        found = talk_of(request, user)
        asked = found if isinstance(found, Response) else await read_json(request, {"title", "system", "thinking", "sampling", "session_mode"})
        if isinstance(asked, Response) or isinstance(found, Response):
            return asked  # type: ignore[return-value]
        title, system, thinking = asked.get("title"), asked.get("system"), asked.get("thinking")
        if not isinstance(title, str | None) or not isinstance(system, str | None) or not isinstance(thinking, bool | None) or title == "":
            return error(400, "title and system are text, and thinking is true or false.")
        try:
            check_sampling(asked.get("sampling", {}))
        except ValueError as problem:
            return error(400, str(problem))
        ref, store, number, _ = found
        if "session_mode" in asked:
            if asked["session_mode"] not in ("fresh", "persistent"):
                return error(400, "session_mode is fresh or persistent.")
            store.session_mode(number, asked["session_mode"])
        store.change(number, title and title[:120], system, thinking, asked.get("sampling"))
        return JSONResponse(shown(ref, store.get(number, user.name) or {}))

    async def conversation_say(request: Request, user: Principal) -> Response:
        """Say something more in a conversation, and set the answer going.

        `do` chooses what: `say` (unless said) adds `content`; `again` throws the last answer
        away and asks anew; `edit` changes the last thing said to `content` and asks anew;
        `continue` asks for more of an answer that was cut off. `model` names who answers.
        """
        found = talk_of(request, user)
        asked = found if isinstance(found, Response) else await read_json(request, {"content", "model", "do", "attachments"})
        if isinstance(asked, Response) or isinstance(found, Response):
            return asked  # type: ignore[return-value]
        ref, store, number, talk = found
        do, content, model, said = asked.get("do", "say"), asked.get("content"), asked.get("model"), talk["messages"]
        if ref in turns:
            return error(409, "This conversation is being answered. Wait for the answer, or stop it.")
        selected = talk.get("compare") or [model]
        if talk.get('compare') and talk.get('unavailable_model_ids'):
            return error(404,'A recorded comparison model was removed or is no longer permitted.')
        for choice in selected:
            if not isinstance(choice, str) or (choice not in offered(user) and not role_of(user, choice)):
                return no_such_model(str(choice), user)
        if talk.get("compare") and do == "continue":
            return error(400, "Use a shared follow-up or Ask both again in a comparison.")
        try:
            files = attachments(asked.get("attachments", []))
        except ValueError as problem:
            return error(400, str(problem))
        history_files = [f for m in said for f in m.get("attachments", [])]
        if any(f["mime"].startswith("image/") for f in [*files, *history_files]) and any(not role_of(user, choice) and not user.options(choice).get("vision") for choice in selected):
            return error(400, "This conversation has images. Choose a local vision model configured with an mmproj projector.")
        if files and do != "say":
            return error(400, "Attach files to a new message. Editing keeps the original attachments.")
        if do in ("say", "edit") and (not isinstance(content, str) or not content.strip()):
            return error(400, "content is what you say.")
        if do == "say":
            store.say(number, "user", content, attachments=files)
        elif do == "edit":
            store.reword(number, content)
            store.session(number, "", None)
        elif do == "again":
            store.drop_answer(number)
            store.session(number, "", None)
        elif do != "continue" or not said or said[-1]["role"] != "assistant" or not said[-1]["content"]:
            return error(400, "do is say, again, edit or continue, and only an answer that has begun can be continued.")
        begin(ref, store, number, request, user, model, carry_on=do == "continue")
        return JSONResponse({"id": ref}, status_code=202)

    async def conversation_stop(request: Request, user: Principal) -> Response:
        """Stop the answer that is on its way. What has come so far is kept."""
        found = talk_of(request, user)
        if isinstance(found, Response):
            return found
        group = turns.get(found[0])
        if not group:
            return error(409, "No answer is on its way in this conversation.")
        for turn in group:
            if not turn.done:
                turn.stopped = True
                if turn.task:
                    turn.task.cancel()
        return JSONResponse({"stopped": found[0]})

    async def conversation_remove(request: Request, user: Principal) -> Response:
        """Cancel active answers and remove the owner's selected conversation."""
        found = talk_of(request, user)
        if isinstance(found, Response):
            return found
        if found[0] in turns:
            return error(409, "This conversation is being answered. Stop that first.")
        found[1].remove(found[2])
        return JSONResponse({"removed": found[0]})

    async def conversation_stream(request: Request, user: Principal) -> Response:
        """The answer that is on its way, as it grows: server-sent events, each with all of it so far.

        It can be joined at any moment, and by several pages at once. Each event holds
        `state`, `content` and `reasoning`. An event named `done` says the answer is kept and
        the conversation can be asked for again. With no answer on its way, that comes at once.
        """
        found = talk_of(request, user)
        if isinstance(found, Response):
            return found
        ref = found[0]

        async def events():
            """Stream incremental chat output and completion state to the authorized browser."""
            group = turns.get(ref, [])
            flights = [None] * len(group)
            reads = [0] * len(group)
            texts = [{"content": "", "reasoning": ""} for _ in group]
            last = None
            while group:
                answers = []
                for i, turn in enumerate(group):
                    flight = None if turn.state else turn.flight
                    if flight is not flights[i]:
                        flights[i], reads[i], texts[i] = flight, 0, {"content": "", "reasoning": ""}
                    if flight:
                        upto = flight.whole.rfind(b"\n") + 1
                        stream_text(bytes(flight.whole[reads[i]:upto]), texts[i])
                        if turn.adaptor:
                            stream_plugin_events(bytes(flight.whole[reads[i]:upto]), texts[i].setdefault("plugin_events", []))
                        reads[i] = upto
                    answers.append({"state": turn.state or ("answering" if flight else "starting"),
                                    "model": turn.model, "done": turn.done, **(turn.answer if turn.done and turn.answer else texts[i])})
                now = {"answers": answers} if found[3].get("compare") else answers[0]
                if now != last:
                    yield f"data: {json.dumps(now)}\n\n"
                    last = now
                if ref not in turns:
                    break
                await asyncio.sleep(.1)
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    def signed_in(handler):
        """Resolve current identity on every operation; never trust a supplied user name."""
        async def checked(request: Request) -> Response:
            """Resolve current account permissions before dispatching a private chat API operation."""
            try:
                response = await compute.get("/loadcoach/me", headers={"authorization": request.headers.get("authorization", "")})
            except httpx.HTTPError:
                return error(503, "Turnstile is not answering.")
            if not response.is_success:
                return error(response.status_code, "Not authorized.")
            info = response.json()
            if plugins:
                info = plugins.enrich(info)
            return await handler(request, Principal(info.get("identity") or info["name"], info))
        return checked

    def route(path, handler, method="GET"):
        """Bind one existing chat API path to authenticated local handling."""
        return Route(path, signed_in(handler), methods=[method])

    async def forget(owner: str) -> None:
        """Remove a deleted account's front-side chats after cancelling its active turns."""
        pending = [turn.task for group in list(turns.values()) for turn in group
                   if turn.owner == owner and turn.task]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        chats.forget(owner)
        passing.forget(owner)
        await workflows.forget(owner)
        await knowledge.forget(owner)

    async def sweep() -> None:
        """Expire temporary conversations after their existing one-day idle limit."""
        while True:
            await asyncio.sleep(3600)
            passing.forget_idle(86400)

    @asynccontextmanager
    async def lifespan(app):
        """Mark abandoned turns, host active work, and retain partial output on shutdown."""
        # Interrupted agent work is never automatically replayed after a process restart.
        knowledge.store.interrupted()
        workflows.store.interrupted()
        for cut in chats.cut_off():
            chats.say(cut["conversation"], "assistant", "", model=cut["model"], branch=cut["branch"],
                      error="WeightRoom stopped during this answer. Ask again to retry.")
            chats.answered(cut["conversation"], cut["branch"])
        sweeping = asyncio.create_task(sweep())
        try:
            yield
        finally:
            sweeping.cancel()
            await asyncio.gather(sweeping, return_exceptions=True)
            pending = [turn.task for group in list(turns.values()) for turn in group if turn.task]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await workflows.close()
            await knowledge.close()
            chats.db.close()
            passing.db.close()

    app = Starlette(lifespan=lifespan, routes=[
        *knowledge.routes(route),
        *workflows.routes(route),
        route("/loadcoach/conversations", conversation_list),
        route("/loadcoach/conversations", conversation_start, "POST"),
        route("/loadcoach/conversations/spoken", conversation_spoken, "POST"),
        route("/loadcoach/conversations/{ref}", conversation_one),
        route("/loadcoach/conversations/{ref}", conversation_change, "POST"),
        route("/loadcoach/conversations/{ref}/messages", conversation_say, "POST"),
        route("/loadcoach/conversations/{ref}/stop", conversation_stop, "POST"),
        route("/loadcoach/conversations/{ref}", conversation_remove, "DELETE"),
        route("/loadcoach/conversations/{ref}/stream", conversation_stream),
    ])
    app.state.chats, app.state.passing, app.state.turns = chats, passing, turns
    app.state.follow = signed_in(conversation_stream)
    app.state.forget = forget
    app.state.knowledge = knowledge
    workflows.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://chat')
    app.state.workflows = workflows
    return app
