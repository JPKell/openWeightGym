"""Account names: login spelling, preserved ownership and atomic renames."""

import asyncio

import pytest

from loadcoach.batches import Batches
from weightroom.chat_store import Conversations
from loadcoach.groups import Group
from loadcoach.users import SignInRefused, Users, code_at


def test_login_case_shares_lockout_and_authenticator_replay_state(monkeypatch):
    users = Users(":memory:", {"plain": Group()})
    users.add("pat", "plain")
    users.change("pat", password="correct horse")
    session = asyncio.run(users.sign_in("PaT", "correct horse"))
    assert users.by_key(session).name == "pat"
    for name in ("PAT", "pat", "Pat", "pAt", "paT"):
        with pytest.raises(SignInRefused) as refused:
            asyncio.run(users.sign_in(name, "wrong"))
        assert refused.value.status == 401
    with pytest.raises(SignInRefused) as refused:
        asyncio.run(users.sign_in("pAT", "correct horse"))
    assert refused.value.status == 429

    users.wrong.clear()
    users.groups.put("plain", {"second_step": True})
    monkeypatch.setattr("loadcoach.users.time.time", lambda: 1800000000)
    with pytest.raises(SignInRefused) as setup:
        asyncio.run(users.sign_in("PAT", "correct horse"))
    code = code_at(setup.value.secret, 1800000000 // 30)
    asyncio.run(users.sign_in("PaT", "correct horse", code))
    with pytest.raises(SignInRefused) as replay:
        asyncio.run(users.sign_in("pat", "correct horse", code))
    assert replay.value.code == "code_wrong"


def test_rename_preserves_credentials_preferences_and_owned_work(tmp_path):
    path = str(tmp_path / "users.db")
    users = Users(path, {"plain": Group()})
    users.add("pat", "plain")
    users.add("app", "plain")
    users.change("pat", password="correct horse", own={"priority": "low"})
    users.choose("pat", {"show_telemetry": False})
    key = users.new_key("pat")
    session = asyncio.run(users.sign_in("pat", "correct horse", via="app"))
    password = users.password_hash("pat")
    identity = users.identity("pat")
    chats, batches = Conversations(str(tmp_path / "chat.db")), Batches(path)
    talk = chats.start(identity, "Keep this conversation")
    chats.answering(talk, 0, identity, "small")
    file = batches.put_file("pat", "input.jsonl", "batch", b"payload")
    batch = batches.start("pat", "/v1/chat/completions", file["id"], [])
    users.wrong["pat"], users.used["pat"] = (3, 0), 42

    users.rename("pat", "alex")
    users.rename("app", "client")
    assert not users.exists("pat") and not users.exists("app")
    assert users.by_key(key).name == "alex"
    assert users.by_key(session).name == "alex" and users.by_key(session).via == "client"
    assert users.password_hash("alex") == password
    assert users.own("alex") == {"priority": "low"}
    assert users.choices("alex")["show_telemetry"] is False
    assert users.wrong["alex"] == (3, 0) and users.used["alex"] == 42
    assert users.identity("alex") == identity
    assert chats.get(talk, identity)["title"] == "Keep this conversation"
    assert chats.get(talk, "pat") is None
    assert chats.cut_off()[0]["user"] == identity
    assert batches.file(file["id"])["user"] == "alex"
    assert batches.get(batch["id"])["user"] == "alex"
    assert users.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert Users(path, {}).by_key(session).name == "alex"
    users.add("pat", "plain")
    assert users.identity("pat") != identity
    assert chats.get(talk, users.identity("pat")) is None


@pytest.mark.parametrize("new", ["", "Has Space", "UPPER", "../other", "x" * 65, "taken"])
def test_bad_rename_leaves_account_and_key_unchanged(new):
    users = Users(":memory:", {"plain": Group()})
    users.add("pat", "plain")
    users.add("taken", "plain")
    key = users.new_key("pat")
    with pytest.raises(ValueError):
        users.rename("pat", new)
    assert users.by_key(key).name == "pat"
    assert users.db.execute("PRAGMA foreign_key_check").fetchall() == []
