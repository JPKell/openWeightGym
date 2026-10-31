"""WeightRoom chat restart retains history and marks interrupted work without replay."""

import sqlite3
import time
from pathlib import Path

from chat_clients import gateway
from test_app import add_user, fake_llama_server, in_an_empty_folder, settled  # noqa: F401


def test_front_chat_restart_keeps_history_and_does_not_replay_work(fake_llama_server, tmp_path):
    source = str(tmp_path / "compute.db")
    with gateway(fake_llama_server, database=source) as caller:
        owner = add_user(caller, "pat", "admin")
        completed = caller.post("/loadcoach/conversations", headers=owner,
            json={"model": "small", "content": "remember cedar"}).json()["id"]
        saved = settled(caller, completed, owner)
        active = caller.post("/loadcoach/conversations", headers=owner,
            json={"model": "small", "content": "slowly count one two three four five six seven eight nine ten eleven twelve"}).json()["id"]
        time.sleep(.5)
        assert caller.get(f"/loadcoach/conversations/{active}", headers=owner).json()["pending"]
    db = sqlite3.connect(str(Path(source).with_suffix(".chat.db")))
    assert db.execute("SELECT COUNT(*) FROM answering").fetchone()[0] == 0
    with gateway(fake_llama_server, database=source) as caller:
        assert settled(caller, completed, owner)["messages"] == saved["messages"]
        interrupted = settled(caller, active, owner)["messages"][-1]
        assert "WeightRoom stopped" in interrupted["error"]
        assert interrupted["content"].startswith("You said:") and "twelve" not in interrupted["content"]
        time.sleep(.2)
        events = caller.get("/loadcoach/events?kind=request&what=asked", headers=owner).json()["events"]
        assert len(events) == 2
        assert not caller.get("/loadcoach/status", headers=owner).json()["running"]
    compute = sqlite3.connect(source)
    assert not compute.execute("SELECT name FROM sqlite_master WHERE name IN ('conversations','messages','answering')").fetchall()
