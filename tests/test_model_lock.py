"""A model locked in place stays loaded until it is unlocked, a restart included."""

from concurrent.futures import ThreadPoolExecutor
import time

from test_app import fake_llama_server, in_an_empty_folder, gateway, add_user  # noqa: F401


def test_a_locked_model_is_not_replaced_or_unloaded_and_the_lock_outlives_a_restart(fake_llama_server, tmp_path):
    database = str(tmp_path / "loadcoach.db")
    with gateway(fake_llama_server, database=database) as client, ThreadPoolExecutor() as pool:
        admin = add_user(client, "first", "admin")
        plain = add_user(client, "plain", "plain", admin)
        lock = lambda on, name="small", who=admin: client.post("/loadcoach/model-lock", json={"name": name, "on": on}, headers=who)  # noqa: E731
        assert lock(True, who=plain).status_code == 403 and lock(True, name="none").status_code == 404
        assert client.post("/v1/chat/completions", json={"model": "small"}, headers=admin).status_code == 200
        assert lock(True).json() == {"name": "small", "locked": True}
        status = client.get("/loadcoach/status", headers=admin).json()
        assert status["loaded"][0]["locked"] and status["busy"] == 0  # not counted as work, so it holds off neither sleep nor a drain

        for asked in ("/loadcoach/unload", "/loadcoach/lock"):  # either would unload every model
            refused = client.post(asked, json={"unload": True}, headers=admin)
            assert refused.status_code == 409 and "small" in refused.json()["error"]["message"]

        replacement = pool.submit(client.post, "/v1/chat/completions", json={"model": "big"}, headers=admin)  # needs the cards small is on
        time.sleep(0.3)
        assert not replacement.done()
        assert lock(False).status_code == 200
        assert replacement.result(timeout=4).status_code == 200
        assert lock(True).status_code == 200  # small is no longer loaded: this loads nothing and is kept for when it is

    with gateway(fake_llama_server, database=database) as client:  # the users and the lock are in the database
        assert client.post("/v1/chat/completions", json={"model": "small"}, headers=admin).status_code == 200
        assert client.get("/loadcoach/status", headers=admin).json()["loaded"][0]["locked"]
        assert client.post("/loadcoach/model-lock", json={"name": "small", "on": False}, headers=admin).status_code == 200
        assert client.post("/loadcoach/unload", headers=admin).json()["loaded"] == []
