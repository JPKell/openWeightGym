"""Held models stay usable while other models, unload and lock wait for expiry."""

from concurrent.futures import ThreadPoolExecutor
import time

from test_app import fake_llama_server, in_an_empty_folder, gateway, add_user, model


def test_hold_expiry_and_requests_for_held_model_pass_waiting_replacement(fake_llama_server):
    with gateway(fake_llama_server) as client, ThreadPoolExecutor() as pool:
        assert client.post("/loadcoach/hold", json={"model": "small", "seconds": .6}).status_code == 200
        assert client.get("/loadcoach/status").json()["busy"] == 1
        replacement = pool.submit(client.post, "/v1/chat/completions", json={"model": "big"})
        time.sleep(.1)
        assert not replacement.done()
        own = client.post("/v1/chat/completions", json={"model": "small"})
        assert own.status_code == 200
        assert replacement.result(timeout=4).status_code == 200
        assert not client.get("/loadcoach/status").json()["holds"]


def test_hold_ownership_renewal_and_drain(fake_llama_server):
    with gateway(fake_llama_server) as client:
        a = add_user(client, "first", "admin")
        b = add_user(client, "second", "admin", a)
        plain = add_user(client, "plain", "plain", a)
        assert client.post("/loadcoach/hold", json={"model": "small", "seconds": 2}, headers=plain).status_code == 403
        assert client.post("/loadcoach/hold", json={"model": "small", "seconds": 121}, headers=a).status_code == 400
        for headers in (a, b):
            assert client.post("/loadcoach/hold", json={"model": "small", "seconds": 2}, headers=headers).status_code == 200
        client.delete("/loadcoach/hold/small", headers=a)
        holds = client.get("/loadcoach/status", headers=b).json()["holds"]
        assert len(holds) == 1 and holds[0]["user"] == "second"
        client.post("/loadcoach/drain", json={}, headers=b)
        assert client.post("/loadcoach/hold", json={"model": "small", "seconds": 2}, headers=b).status_code == 200
        assert client.post("/loadcoach/hold", json={"model": "big", "seconds": 2}, headers=a).status_code == 503


def test_other_card_can_work_and_unload_waits(fake_llama_server):
    with gateway(fake_llama_server, models={"small": model("small.gguf", ["GPU0"]), "big": model("big.gguf", ["GPU1"])}) as client, ThreadPoolExecutor() as pool:
        assert client.post("/loadcoach/hold", json={"model": "small", "seconds": .6}).status_code == 200
        assert client.post("/v1/chat/completions", json={"model": "big"}).status_code == 200
        unloading = pool.submit(client.post, "/loadcoach/unload")
        time.sleep(.1)
        assert not unloading.done()
        assert unloading.result(timeout=4).status_code == 200
