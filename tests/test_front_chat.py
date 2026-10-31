"""Front-side chat behavior against compute's ordinary authenticated model APIs."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from chat_clients import gateway
from test_app import add_user, model, settled, LEVELS_OF, fake_llama_server, in_an_empty_folder  # noqa: F401


@pytest.mark.parametrize('comparison',[False,True])
def test_model_rename_preserves_chat_identity_and_comparison_branches(fake_llama_server,tmp_path,comparison):
    database=str(tmp_path/'models.db')
    models={'small':model('small.gguf'),'other':model('other.gguf')}
    with gateway(fake_llama_server,models=models,database=database) as caller:
        extra={'compare_model':'other'} if comparison else {}
        ref=caller.post('/loadcoach/conversations',json={'content':'hello','model':'small',**extra}).json()['id']
        before=settled(caller,ref,{})
        identity=before['messages'][1]['model_id']
        with sqlite3.connect(Path(database).with_suffix('.chat.db')) as db:
            recorded=db.execute('SELECT content,detail FROM messages WHERE role="assistant"').fetchall()
        renamed=caller.post('/loadcoach/model-rename',json={'name':'small','to':'new-name'})
        assert renamed.status_code==200,renamed.text
        after=caller.get(f'/loadcoach/conversations/{ref}').json()
        answer=next(m for m in after['messages'] if m.get('model_id')==identity)
        assert answer['model']=='new-name' and answer['model_at_reply']=='small'
        if comparison:
            assert after['compare']==['new-name','other']
        with sqlite3.connect(Path(database).with_suffix('.chat.db')) as db:
            assert db.execute('SELECT content,detail FROM messages WHERE role="assistant"').fetchall()==recorded
        reply=caller.post(f'/loadcoach/conversations/{ref}/messages',json={'content':'follow up','model':'new-name'})
        assert reply.status_code==202,reply.text
        final=settled(caller,ref,{})
        assert identity in {m.get('model_id') for m in final['messages'][-(2 if comparison else 1):]}

@pytest.mark.parametrize("in_memory", [False, True])
def test_renamed_account_keeps_saved_and_temporary_conversations(fake_llama_server, tmp_path, in_memory):
    database = ":memory:" if in_memory else str(tmp_path / "users.db")
    with gateway(fake_llama_server, database=database, unlogged=True) as caller:
        admin = add_user(caller, "jordan", "admin")
        conversations = []
        for temporary in (False, True):
            made = caller.post("/loadcoach/conversations", headers=admin,
                               json={"content": "hello", "model": "small", "temporary": temporary})
            assert made.status_code == 201, made.text
            ref = made.json()["id"]
            caller.get(f"/loadcoach/conversations/{ref}/stream", headers=admin)
            conversations.append(ref)
        renamed = caller.post("/loadcoach/users/jordan/rename", headers=admin, json={"name": "alex"})
        assert renamed.status_code == 200, renamed.text
        for ref in conversations:
            talk = caller.get(f"/loadcoach/conversations/{ref}", headers=admin)
            assert talk.status_code == 200 and talk.json()["messages"][0]["content"] == "hello"



def test_a_conversation_is_kept_answered_from_inside_and_only_its_owner_has_it(fake_llama_server):
    with gateway(fake_llama_server, groups=LEVELS_OF) as caller:
        owner = add_user(caller, "sam", "owner")
        pat = add_user(caller, "pat", "plain", by=owner)
        say = lambda ref, **sent: caller.post(f"/loadcoach/conversations/{ref}/messages", json={"model": "small", **sent}, headers=pat)  # noqa: E731

        started = caller.post("/loadcoach/conversations", json={"content": "Please think about tea\nand cake", "model": "small", "system": "Be brief.", "sampling": {"temperature": 0.2, "top_k": 20}}, headers=pat)
        ref = started.json()["id"]
        first = settled(caller, ref, pat)
        answer = first["messages"][1]
        sent = caller.get(f"/loadcoach/requests/{answer['request']}", headers=pat).json()
        theirs = [caller.get(f"/loadcoach/conversations/{ref}", headers=owner).status_code, caller.get("/loadcoach/conversations", headers=owner).json()["conversations"]]
        listed = caller.get("/loadcoach/conversations", headers=pat).json()["conversations"]

        # How the model picks its words is the conversation's, and only numbers that make sense are taken.
        set_how = lambda **sampling: caller.post(f"/loadcoach/conversations/{ref}", json={"sampling": sampling}, headers=pat).status_code  # noqa: E731
        how = [set_how(temperature=9), set_how(top_k=1.5), set_how(top_p="half"), set_how(colour=1), set_how(min_p=0.05), caller.get(f"/loadcoach/conversations/{ref}", headers=pat).json()["sampling"]]
        # Asked not to think, and given a new name.
        caller.post(f"/loadcoach/conversations/{ref}", json={"thinking": False, "title": "Tea"}, headers=pat)
        say(ref, content="think again")
        second = settled(caller, ref, pat)
        unthinking = caller.get(f"/loadcoach/requests/{second['messages'][3]['request']}", headers=pat).json()["text"]["asked"]

        # While an answer is on its way: nothing more can be said, and it can be followed as it grows, by anyone who joins.
        say(ref, content="answer slowly one two three")
        busy = say(ref, content="and another thing").status_code
        followed = caller.get(f"/loadcoach/conversations/{ref}/stream", headers=pat).text
        grown = [json.loads(line[6:]) for line in followed.splitlines() if line.startswith("data: {") and "content" in line]
        third = settled(caller, ref, pat)

        # Stopped part-way, what had come is kept. Then more of it is asked for, and the two parts are one answer.
        say(ref, content="count slowly one two three four five six seven eight nine ten")
        time.sleep(0.7)  # a few words in
        assert caller.post(f"/loadcoach/conversations/{ref}/stop", headers=pat).status_code == 200
        stopped = settled(caller, ref, pat)["messages"][-1]
        say(ref, do="continue")
        carried_on = settled(caller, ref, pat)["messages"][-1]

        # Asked again, and the last thing said changed: in both the answer is replaced, not added to.
        say(ref, do="again")
        again = settled(caller, ref, pat)["messages"]
        say(ref, do="edit", content="something else")
        edited = settled(caller, ref, pat)["messages"]
        wrong = [say(ref, content="  ").status_code, say(ref, do="dance").status_code, caller.post(f"/loadcoach/conversations/{ref}/messages", json={"model": "none", "content": "x"}, headers=pat).status_code,
                 caller.post("/loadcoach/conversations", json={"content": "x", "model": "small", "temporary": True}, headers=owner).status_code]  # fmt: skip
        its_requests = caller.get("/loadcoach/events", params={"trace": f"chat-{ref}", "what": "asked"}, headers=owner).json()["events"]
        gone = [caller.delete(f"/loadcoach/conversations/{ref}", headers=pat).status_code, caller.get(f"/loadcoach/conversations/{ref}", headers=pat).status_code]

    assert started.status_code == 201 and first["title"] == "Please think about tea" and first["system"] == "Be brief." and not first["temporary"]
    assert [message["role"] for message in first["messages"]] == ["user", "assistant"] and answer["content"].strip() == "You said: Please think about tea\nand cake"
    assert (answer["reasoning"], answer["model"], answer["finish"]) == ("Let me think. Done thinking.", "small", "stop")
    assert answer["prompt_tokens"] == 8 and answer["answer_tokens"] == 8 and answer["request"] > 0  # the figures under an answer, from the log
    # It was an ordinary request of pat's own, with the instructions first and the conversation as its piece of work.
    assert sent["asked"]["user"] == "pat" and sent["asked"]["trace"] == f"chat-{ref}" and sent["text"]["asked"]["messages"][0] == {"role": "system", "content": "Be brief."}
    assert (sent["text"]["asked"]["temperature"], sent["text"]["asked"]["top_k"]) == (0.2, 20) and how == [400, 400, 400, 400, 200, {"min_p": 0.05}]  # sent with every request, by the names the model server knows
    assert theirs == [404, []] and [(talk["id"], talk["title"]) for talk in listed] == [(ref, "Please think about tea")]  # nobody else has it, whatever they may do
    assert second["title"] == "Tea" and "reasoning" not in second["messages"][3] and unthinking["chat_template_kwargs"] == {"enable_thinking": False}
    assert busy == 409 and len(grown) > 2 and grown[0]["content"] != grown[-1]["content"] and grown[-1]["content"].strip().endswith("three")
    assert followed.rstrip().endswith("event: done\ndata: {}") and third["messages"][-1]["content"].strip().endswith("three")
    assert stopped["content"].startswith("You said: count") and "ten" not in stopped["content"] and "finish" not in stopped
    assert carried_on["content"] == stopped["content"] + " and so on. " and carried_on["finish"] == "stop"
    assert len(again) == 8 and again[-1]["content"].strip().endswith("ten") and len(edited) == 8 and edited[-2]["content"] == "something else" and edited[-1]["content"].strip() == "You said: something else"
    assert wrong == [400, 400, 404, 403] and len(its_requests) == 7 and gone == [200, 404]



def test_an_impossibly_small_context_fails_without_silent_cuts_and_a_temporary_chat_leaves_nothing(fake_llama_server, tmp_path):
    # A model that holds sixty words. For the stand-in, a word is a token.
    tight = {"small": model("small.gguf", **{"ctx-size": 60})}
    long = " ".join(["word"] * 20)
    with gateway(fake_llama_server, tight) as caller:
        say = lambda ref, content: caller.post(f"/loadcoach/conversations/{ref}/messages", json={"model": "small", "content": content})  # noqa: E731
        ref = caller.post("/loadcoach/conversations", json={"content": "hi", "model": "small"}).json()["id"]
        settled(caller, ref, {})
        for words in ("hello", long, long):  # the fourth question no longer fits with everything before it
            say(ref, words)
            talk = settled(caller, ref, {})
        cut = talk["messages"][-1]
        say(ref, " ".join(["word"] * 100))  # and one that is too long all by itself
        hopeless = settled(caller, ref, {})["messages"][-1]

    assert "too small" in cut["error"] and len(talk["messages"]) == 8
    assert "too small" in hopeless["error"] and hopeless["content"] == ""

    file, quiet = str(tmp_path / "loadcoach.db"), {**LEVELS_OF}
    rows = lambda table: sqlite3.connect(str(Path(file).with_suffix(".chat.db")) if table in ("conversations", "messages") else file).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
    with gateway(fake_llama_server, groups=quiet, database=file, unlogged=True) as caller:
        owner = add_user(caller, "sam", "owner")
        pat = add_user(caller, "pat", "plain", by=owner)
        kept = caller.post("/loadcoach/conversations", json={"content": "keep this", "model": "small"}, headers=pat).json()["id"]
        settled(caller, kept, pat)
        before = rows("events"), rows("conversations"), rows("texts")
        passing = caller.post("/loadcoach/conversations", json={"content": "between us", "model": "small", "temporary": True}, headers=owner).json()["id"]
        answered = settled(caller, passing, owner)
        after = rows("events"), rows("conversations"), rows("texts")
        listed = caller.get("/loadcoach/conversations", headers=owner).json()["conversations"]
        refused = caller.post("/loadcoach/conversations", json={"content": "x", "model": "small", "temporary": True}, headers=pat).status_code
        caller.delete(f"/loadcoach/conversations/{passing}", headers=owner)
        caller.delete("/loadcoach/users/pat", headers=owner)
        left = rows("conversations"), rows("messages"), caller.get("/loadcoach/conversations", headers=owner).json()["conversations"]

    assert passing.startswith("t") and answered["temporary"] and answered["messages"][1]["content"].strip() == "You said: between us"
    assert "request" not in answered["messages"][1] and after == before  # answered, and not a row more: no entry, no kept text, no conversation in the file
    assert [(talk["id"], talk.get("temporary")) for talk in listed] == [(passing, True)] and refused == 403
    assert left == (0, 0, [])  # removed with a click, and a removed account's conversations go with it



def test_chat_summarizes_before_context_fills_and_keeps_originals(fake_llama_server):
    with gateway(fake_llama_server, {"small": model("small.gguf", **{"ctx-size": 512})}) as caller:
        ref = caller.post("/loadcoach/conversations", json={"content": "Remember cedar. " + "detail " * 50, "model": "small"}).json()["id"]
        settled(caller, ref, {})
        for question in ["What else? " + "more " * 40, "What did I ask you to remember?"]:
            caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": question, "model": "small"})
            talk = settled(caller, ref, {})
        assert talk["memory"]["covered"] == 2 and "cedar" in talk["memory"]["text"]
        assert len(talk["messages"]) == 6 and "detail " * 50 in talk["messages"][0]["content"]
        answer = talk["messages"][-1]
        sent = caller.get(f"/loadcoach/requests/{answer['request']}").json()["text"]["asked"]["messages"]
        assert "summary" in sent[0]["content"] and "cedar" in sent[0]["content"]
        assert sent[-1]["content"] == "What did I ask you to remember?"
        assert answer["summarized"] == 2 and "error" not in answer
        events = caller.get(f"/loadcoach/events?trace=chat-{ref}&what=asked").json()["events"]
        assert len(events) > 3  # summaries use the ordinary authenticated and logged request path



def test_failed_summary_keeps_the_full_history_and_reports_failure(fake_llama_server):
    with gateway(fake_llama_server, {"small": model("small.gguf", **{"ctx-size": 512})}) as caller:
        ref = caller.post("/loadcoach/conversations", json={"content": "FAIL SUMMARY " + "detail " * 50, "model": "small"}).json()["id"]
        settled(caller, ref, {})
        for question in ["More " + "detail " * 30, "Continue please"]:
            caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": question, "model": "small"})
            talk = settled(caller, ref, {})
        assert not talk["memory"] and len(talk["messages"]) == 6
        assert "Could not finish" in talk["messages"][-1]["error"]
        assert talk["messages"][0]["content"].startswith("FAIL SUMMARY")



def test_chat_attachments_are_validated_private_and_reach_the_model(fake_llama_server):
    import base64
    import io
    from PIL import Image
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, format="PNG")
    picture = {"name": "red.png", "data": base64.b64encode(image.getvalue()).decode()}
    document = {"name": "notes.txt", "data": base64.b64encode(b"Secret cedar text <script>").decode()}
    with gateway(fake_llama_server, {"small": model("small.gguf"), "vision": model("vision.gguf", **{"ctx-size": 8192, "mmproj": "vision-projector.gguf"})}) as caller:
        owner = add_user(caller, "sam", "admin")
        pat = add_user(caller, "pat", "plain", by=owner)
        def start(files, model="small", who=pat):
            return caller.post("/loadcoach/conversations", json={"content": "Explain these", "model": model, "attachments": files}, headers=who)
        assert start([picture]).status_code == 400
        assert start([{"name": "bad", "data": "not base64"}]).status_code == 400
        assert start([document] * 5).status_code == 400
        assert start([{"name": "x.pdf", "data": base64.b64encode(b"%PDF-1.5").decode()}]).status_code == 400
        assert start([{"name": "notes", "data": base64.b64encode(b"x" * (200 * 1024 + 1)).decode()}]).status_code == 400
        started = start([picture, document], "vision")
        assert started.status_code == 201, started.text
        ref = started.json()["id"]
        talk = settled(caller, ref, pat)
        assert talk["messages"][0]["attachments"][0]["mime"] == "image/png"
        assert "error" not in talk["messages"][-1]
        sent = caller.get(f"/loadcoach/requests/{talk['messages'][-1]['request']}", headers=pat).json()["text"]["asked"]["messages"][-1]["content"]
        assert "Secret cedar" in sent[0]["text"] and sent[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert caller.get(f"/loadcoach/conversations/{ref}", headers=owner).status_code == 404
        assert caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": "more", "model": "small"}, headers=pat).status_code == 400
        caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": "Reworded", "model": "vision", "do": "edit"}, headers=pat)
        assert len(settled(caller, ref, pat)["messages"][0]["attachments"]) == 2
        caller.delete(f"/loadcoach/conversations/{ref}", headers=pat)
        assert caller.get(f"/loadcoach/conversations/{ref}", headers=pat).status_code == 404



def test_comparison_keeps_histories_separate_streams_both_and_edits_both(fake_llama_server):
    models = {"small": model("small.gguf", gpus=["GPU0"], **{"answer-prefix": "ALPHA"}),
              "big": model("big.gguf", gpus=["GPU1"], **{"answer-prefix": "BETA"})}
    with gateway(fake_llama_server, models) as caller:
        start = lambda **more: caller.post("/loadcoach/conversations", json={"content": "hello", "model": "small", **more})
        assert start(compare_model="small").status_code == 400
        assert start(compare_model="missing").status_code == 404
        ref = start(compare_model="big").json()["id"]
        first = settled(caller, ref, {})
        assert first["compare"] == ["small", "big"] and len(first["messages"]) == 3
        assert first["exchanges"][0]["answers"][0]["content"].startswith("ALPHA")
        assert first["exchanges"][0]["answers"][1]["content"].startswith("BETA")
        url = f"/loadcoach/conversations/{ref}/messages"
        caller.post(url, json={"content": "slowly explain the next step"})
        assert caller.post(url, json={"content": "another"}).status_code == 409
        streamed = caller.get(f"/loadcoach/conversations/{ref}/stream").text
        assert '"answers":' in streamed and '"model": "small"' in streamed and '"model": "big"' in streamed
        second = settled(caller, ref, {})
        for branch, marker in enumerate(["ALPHA", "BETA"]):
            reply = second["exchanges"][-1]["answers"][branch]
            sent = caller.get(f"/loadcoach/requests/{reply['request']}").json()["text"]["asked"]["messages"]
            assert [m["role"] for m in sent] == ["user", "assistant", "user"]
            assert sent[1]["content"].startswith(marker)
            assert sent[-1]["content"] == "slowly explain the next step"
        caller.post(url, json={"do": "edit", "content": "replacement"})
        edited = settled(caller, ref, {})
        assert len(edited["messages"]) == 6 and all("replacement" in m["content"] for m in edited["exchanges"][-1]["answers"])
        caller.post(url, json={"do": "again"})
        assert len(settled(caller, ref, {})["messages"]) == 6
        caller.post(url, json={"content": "slowly count one two three four five six seven eight nine"})
        time.sleep(.5)
        assert caller.post(f"/loadcoach/conversations/{ref}/stop").status_code == 200
        stopped = settled(caller, ref, {})
        assert len(stopped["messages"]) == 9 and all("nine" not in m["content"] for m in stopped["exchanges"][-1]["answers"])



def test_one_failed_comparison_model_does_not_lose_the_other_reply(fake_llama_server):
    with gateway(fake_llama_server, {"small": model("small.gguf"), "broken": model("broken.gguf", fail=True)}) as caller:
        ref = caller.post("/loadcoach/conversations", json={"content": "hello", "model": "small", "compare_model": "broken"}).json()["id"]
        talk = settled(caller, ref, {})
        left, right = talk["exchanges"][0]["answers"]
        assert "hello" in left["content"] and "error" not in left and right["error"]



def test_comparison_summaries_are_independent_and_temporary_uploads_stay_in_memory(fake_llama_server, tmp_path):
    import base64
    file = str(tmp_path / "chat-check.db")
    models = {name: model(name + ".gguf", gpus=0, **{"ctx-size": 512}) for name in ["small", "big"]}
    with gateway(fake_llama_server, models, database=file, unlogged=True) as caller:
        ref = caller.post("/loadcoach/conversations", json={"content": "Remember cedar " + "detail " * 50,
            "model": "small", "compare_model": "big", "temporary": True,
            "attachments": [{"name": "secret.txt", "data": base64.b64encode(b"private notes").decode()}]}).json()["id"]
        settled(caller, ref, {})
        for words in ["More " + "detail " * 30, "What is the codename?"]:
            caller.post(f"/loadcoach/conversations/{ref}/messages", json={"content": words})
            talk = settled(caller, ref, {})
        assert set(talk["memory"]) == {"0", "1"}
        assert talk["memory"]["0"]["model"] == "small" and talk["memory"]["1"]["model"] == "big"
        assert all("error" not in m for m in talk["exchanges"][-1]["answers"])
        db = sqlite3.connect(file)
        assert db.execute("SELECT COUNT(*) FROM texts").fetchone()[0] == 0
        front = sqlite3.connect(str(Path(file).with_suffix(".chat.db")))
        assert all(front.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ["conversations", "messages"])
        assert not db.execute("SELECT name FROM sqlite_master WHERE name = 'conversations'").fetchone()
        assert caller.get("/loadcoach/events?kind=request").json()["events"] == []

