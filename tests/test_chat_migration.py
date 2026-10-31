"""Conversation migration preserves private content and refuses unsafe retirement."""

import os
import sqlite3

import pytest

from weightroom.chat_migration import export, retire, verify
from weightroom.chat_store import Conversations
from loadcoach.groups import Group
from loadcoach.users import Users


def legacy(tmp_path):
    """Make a legacy compute database containing chats beside credentials and model facts."""
    source = tmp_path / "compute.db"
    users = Users(str(source), {"plain": Group()})
    users.add("pat", "plain")
    users.new_key("pat")
    users.change("pat", password="private password")
    chats = Conversations(str(source))
    number = chats.start("pat", "Keep cedar", system="Be brief", sampling={"temperature": .2}, compare=["one", "two"])
    chats.say(number, "user", "cedar", attachments=[{"name": "note.txt", "text": "private attachment"}])
    chats.say(number, "assistant", "answer", reasoning="returned reasoning", request=42, branch=0)
    chats.remember(number, {"0": {"text": "summary", "covered": 2}})
    with users.db:
        users.db.execute("CREATE TABLE model_facts (name TEXT)")
        users.db.execute("INSERT INTO model_facts VALUES ('untouched')")
    return source, users, chats, number


def test_exact_chat_only_copy_and_verified_retirement(tmp_path):
    source, users, chats, number = legacy(tmp_path)
    copy = tmp_path / "front-chat.db"
    report = export(source, copy)
    assert report["counts"] == {"conversations": 1, "messages": 2, "answering": 0}
    assert verify(source, copy) == report
    assert os.stat(copy).st_mode & 0o777 == 0o600
    front = Conversations(str(copy))
    original = chats.get(number, "pat")
    assert front.get(number, users.identity("pat")) == original
    assert front.get(number, "pat") is None
    tables = {row[0] for row in front.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"conversations", "messages", "answering", "sqlite_sequence", "suite_schema_migrations",
                      "collections", "documents", "document_chunks", "prompt_versions", "workflows", "workflow_attempts"}
    assert not front.db.execute('SELECT id FROM collections').fetchall()
    retire(source, copy)
    assert users.by_name("pat") is not None
    assert users.db.execute("SELECT * FROM model_facts").fetchall() == [("untouched",)]
    assert not users.db.execute("SELECT name FROM sqlite_master WHERE name IN ('conversations','messages','answering')").fetchall()
    assert front.get(number, users.identity("pat")) == original


def test_new_or_changed_records_prevent_retirement(tmp_path):
    source, users, chats, number = legacy(tmp_path)
    copy = tmp_path / "front-chat.db"
    export(source, copy)
    chats.say(number, "user", "new turn arrived")
    with pytest.raises(ValueError, match="differ"):
        verify(source, copy)
    with pytest.raises(ValueError, match="changed"):
        retire(source, copy)
    assert len(chats.get(number, "pat")["messages"]) == 3
    assert users.by_name("pat")


def test_active_work_or_existing_destination_prevents_export(tmp_path):
    source, _, chats, number = legacy(tmp_path)
    copy = tmp_path / "front-chat.db"
    chats.answering(number, 0, "pat", "one")
    with pytest.raises(ValueError, match="active"):
        export(source, copy)
    assert not copy.exists()
    chats.answered(number, 0)
    export(source, copy)
    with pytest.raises(ValueError, match="already exists"):
        export(source, copy)
    assert sqlite3.connect(copy).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_chat_copy_refuses_populated_document_collections(tmp_path):
    from weightroom.knowledge_store import Collections
    from test_collections import EMBED
    source,users,chats,number=legacy(tmp_path)
    copied=tmp_path/'before-collections.db'
    export(source,copied)
    collections=Collections(str(source))
    collections.create(users.identity('pat'),'Private sources','normal',EMBED)
    with pytest.raises(ValueError,match='document collections'):
        export(source,tmp_path/'not-created.db')
    with pytest.raises(ValueError,match='document collections'):
        retire(source,copied)
    assert not (tmp_path/'not-created.db').exists()
    assert chats.get(number,'pat')['title']=='Keep cedar'
    collections.db.close()
