"""Status capacity and loaded-model table use the multi-LoadCoach inventory."""

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from weightroom.app import memory_size, size, status_nodes, worded


def test_cpu_loadcoach_uses_fresh_spotter_ram_from_its_physical_host():
    reading = {"memory": {"total": 16 * 2**30, "used": 4 * 2**30}}
    inventory = {"machines": [{"name": "front_cpu", "fingerprint": "same-host", "telemetry_age": None}],
                 "hosts": [{"fingerprint": "same-host", "telemetry": reading, "age": 2}]}
    assert status_nodes(inventory, 10)[0]["telemetry"] == reading
    inventory["hosts"][0]["age"] = 40
    assert "telemetry" not in status_nodes(inventory, 10)[0]


def test_current_combined_status_updates_each_named_node_without_reviving_an_offline_node():
    inventory = {"machines": [
        {"name": "compute", "state": "ready", "status_live": True},
        {"name": "worker", "state": "ready", "status_live": True},
        {"name": "offline", "state": "not answering", "status_live": False,
         "loaded": [{"model": "old", "cards": ["CUDA2"]}]},
    ]}
    now = {"memory": {"compute:CUDA0": {"total": 16000, "free": 8000, "others": 500},
                      "worker:CUDA1": {"total": 16000, "free": 4000}},
           "loaded": [{"machine": "worker", "model": "new", "state": "ready", "gpus": ["CUDA1"], "locked": True}]}
    compute, worker, offline = status_nodes(inventory, 10, now)
    assert compute["cards"][0]["card"] == "CUDA0"
    assert compute["cards"][0]["others"] == 500
    assert worker["cards"][0]["free"] == 4000 and worker["loaded"][0]["model"] == "new"
    assert offline["status_live"] is False
    assert [node["name"] for node in status_nodes({}, 10, now)] == ["compute", "worker"]


def test_status_shows_gpu_cpu_and_unavailable_loadcoaches_with_scoped_locks():
    pages = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "weightroom/pages"),
                        autoescape=select_autoescape())
    pages.filters["memory_mib"] = lambda count: memory_size(count * 2**20 if isinstance(count, (int, float)) else None)
    pages.filters["memory_size"] = memory_size
    pages.filters["mib"] = lambda count: size(count * 2**20)
    pages.filters["worded"] = lambda value, unit: worded(unit, value)
    nodes = [
        {"name": "compute", "hostname": "jpk-main", "state": "ready", "status_live": True,
         "cards": [{"card": "CUDA0", "total": 16000, "free": 6000, "others": 1000}],
         "loaded": [{"model": "gpu-model", "state": "ready", "cards": ["CUDA0"], "mib": [9000], "locked": False}],
         "telemetry": None, "telemetry_age": None},
        {"name": "front_cpu", "hostname": "k-srv", "state": "ready", "status_live": True,
         "cards": [], "loaded": [{"model": "embedder", "state": "ready", "cards": [], "mib": None, "rss_mib": 384, "locked": True}],
         "cpu": {"logical":4,"concurrency":1}, "running":0, "waiting":2,
         "system_memory":{"free":1500,"headroom":512,"service_limit":4096},
         "telemetry": {"memory": {"total": 16 * 2**30, "used": 4 * 2**30}}, "telemetry_age": 2},
        {"name": "remote", "hostname": "jpk-remote", "state": "not answering", "status_live": False,
         "cards": [], "loaded": [], "telemetry": None, "telemetry_age": None},
    ]
    html = pages.get_template("status_nodes.html").render(
        nodes=nodes, poll_seconds=10, now={}, seen=None,
        person=SimpleNamespace(can={"unload", "manage-models"}), address=lambda name: "/models/" + name)
    capacity = html.split('Loaded models</h2>', 1)[0]
    assert 'badge text-bg-secondary me-1' not in capacity
    assert 'compute capacity' in html and 'front_cpu capacity' in html and 'remote capacity' in html
    assert 'CUDA0' in html and 'gpu-model' in html and '5.9 GiB free for a model' in html
    assert 'Other programs: 1000 MiB' in html and 'other programs 1000 MiB' in html
    assert 'title="gpu-model: 8.8 GiB"' in html and 'bg-secondary' in html
    assert 'CPU' in html and 'embedder' in html and '12.0 GiB available' in html
    assert 'system RAM used, including openWeightGym' in html
    assert '384 MiB RSS' in html and 'logical CPUs' not in html and '2 queued' in html
    assert 'Admission 1.5 GiB free' in html and '512 MiB host reserve' in html
    assert '4.0 GiB service ceiling' in html
    assert 'Loaded models unavailable.' in html
    assert 'name="machine" value="compute"' in html and 'name="machine" value="front_cpu"' in html
    assert 'aria-label="Unlock embedder on front_cpu"' in html


def test_memory_units_are_binary_and_explicit():
    assert memory_size(512*2**20)=='512 MiB'
    assert memory_size(2**30)=='1.0 GiB'
    assert memory_size(None)=='unavailable'


def test_status_unknown_ram_fields_remain_unavailable():
    pages = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "weightroom/pages"),
                        autoescape=select_autoescape())
    pages.filters["memory_mib"] = lambda count: memory_size(count * 2**20 if isinstance(count, (int, float)) else None)
    pages.filters["memory_size"] = memory_size
    node = {"name":"cpu", "state":"ready", "status_live":True, "cards":[],
            "loaded":[{"model":"embedder","cards":[],"state":"ready","rss_mib":None}],
            "telemetry":None,"telemetry_age":None,"system_memory":{"free":None,"headroom":None,"service_limit":None}}
    html=pages.get_template("status_nodes.html").render(nodes=[node],poll_seconds=10,now={},seen=None,
        person=SimpleNamespace(can=set()),address=lambda name:"/models/"+name)
    assert "RAM unavailable" in html and "RSS unavailable" in html
    assert "Admission unavailable free" in html and "unavailable host reserve" in html
    assert "0 MiB" not in html


def test_status_capacity_honors_model_devices_and_preserves_unknown_memory():
    memory = {'compute:CUDA0': None, 'compute:AMD@0000:65:00.0': None,
              'worker:Intel@0000:00:02.0': None}
    inventory = {'machines': [
        {'name':'compute', 'state':'ready', 'status_live':True},
        {'name':'worker', 'state':'ready', 'status_live':True,
         'cards':[{'card':'Intel@0000:00:02.0'}]}]}
    nodes = status_nodes(inventory, 10, {'memory':memory, 'model_cards':['compute:CUDA0']})
    assert nodes[0]['cards'] == [{'card':'CUDA0','total':None,'free':None,'others':None}]
    assert nodes[1]['cards'] == []
    # Older workers without policy metadata retain their unknown-memory devices.
    assert len(status_nodes({},10,{'memory':memory})[0]['cards']) == 2
