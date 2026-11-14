"""Checks of settings changed from the site, on the side of the programs that take them: see loadcoach/settings.py.

LoadCoach's own side, which keeps the changes and hands them out, is checked in test_app.py,
and the page in test_weightroom.py.
"""

import asyncio
import json

import httpx
import pytest

import turnstile.app as turnstile
import spotter.app as spotter
import toollocker.door as toollocker
from loadcoach.settings import CHANGEABLE, Changes, comments, read_value, said, taken, with_changes, written, wrong_kind


def test_a_value_is_written_and_read_as_its_file_holds_it_and_replaced_only_by_its_own_kind():
    table = {"default": {"model": "small", "modes": ["direct"], "speaker": 3}, "night shift": {"thinking": False}}
    for value in (50, 0.5, True, "a \"quoted\" word", ["/", "/mnt/ssd"], table):
        assert read_value(written(value), isinstance(value, dict)) == value
    assert written(table).splitlines()[:2] == ["[default]", 'model = "small"'] and '["night shift"]' in written(table)
    with pytest.raises(ValueError, match="not written as a settings file would hold it"):
        read_value("many", False)
    assert [wrong_kind(new, old) for new, old in ((7, 50), (7.5, 0.5), (7, 0.5), ("x", "y"), ({}, {"a": 1}))] == [None] * 5
    assert "a number, and this is text" in wrong_kind("7", 50) and "whole number" in wrong_kind(7.5, 50) and "less than nothing" in wrong_kind(-1, 50)
    assert "true or false" in wrong_kind(1, True)  # a tick is not a number, though Python would let it pass for one

    file = '# Seconds between readings.\n# Kept in spotter.db.\nsample_every = 30\n\nkeep_days = 28\n\n# What may be restarted.\n[services]\nloadcoach = "loadcoach.service"\n'
    assert comments(file) == {"sample_every": "Seconds between readings. Kept in spotter.db.", "keep_days": "", "services": "What may be restarted.", "loadcoach": ""}
    # What a program says of itself never holds a secret, and says of each setting whether its file names it.
    told = said({"port": 8440, "key": "lc_secret", "twilio": {"auth_token": "t", "number": "+1"}, "wait": 180}, {"port": 8440}, {"port": "Where it listens."})
    assert told["key"]["value"] == "(not shown)" and told["twilio"]["value"] == {"auth_token": "(not shown)", "number": "+1"}
    assert told["port"] == {"value": 8440, "says": "Where it listens.", "file": 8440} and "file" not in told["wait"]


def test_what_was_changed_lies_over_spotters_file_and_is_taken_on_while_it_runs(tmp_path, capsys):
    (tmp_path / "loadcoach.toml").write_text("")  # LoadCoach's own file: the database is beside it
    file = tmp_path / "spotter.toml"
    file.write_text('# Minutes unused before sleep.\nsleep_after = 0\n\n[services]\nloadcoach = "loadcoach.service"\n')
    settings = spotter.load_settings(file)
    changes = Changes(settings.database)
    changes.put("spotter", "sleep_after", 45, "jordan")
    changes.put("spotter", "host", "0.0.0.0", "someone")  # not among what may be changed: it is passed over, wherever it came from
    running, refused = taken("spotter", settings, changes.of("spotter"), spotter.settings_from)
    assert (settings.sleep_after, running.sleep_after, running.host, refused) == (0, 45, "127.0.0.1", {}) and running.file == {"sleep_after": 0, "services": {"loadcoach": "loadcoach.service"}}

    # What spotter's own rules refuse is not taken on, and the reason goes back to the page.
    changes.put("spotter", "sample_every", 0, "jordan")
    unchanged, refused = taken("spotter", running, changes.of("spotter"), spotter.settings_from)
    assert unchanged is running and "sample_every must be at least 1" in refused["sample_every"]
    # At the next start a change that cannot be used does not keep spotter from starting. Without it, the others hold.
    assert spotter.load_settings(file).sleep_after == 0 and "cannot be used" in capsys.readouterr().err
    changes.take_away("spotter", "sample_every")
    assert spotter.load_settings(file).sleep_after == 45 and spotter.load_settings(file).says["sleep_after"] == "Minutes unused before sleep."


def test_the_tool_door_and_turnstile_start_with_what_was_changed(tmp_path):
    (tmp_path / "loadcoach.toml").write_text("")
    door = tmp_path / "toollocker.toml"
    door.write_text("call_timeout = 120\n")
    Changes(str(tmp_path / "loadcoach.db")).put("toollocker", "call_timeout", 30, "jordan")
    config = toollocker.load_config(door)
    assert (config.call_timeout, config.file) == (30, {"call_timeout": 120})
    assert "above 0" in taken("toollocker", config, {"call_timeout": 0}, toollocker.config_from)[1]["call_timeout"]

    # turnstile has no key of its own. It reads the copy weightroom keeps, and leaves what it runs with for weightroom to pass on.
    front = tmp_path / "weightroom.toml"
    front.write_text('compute = "http://10.0.0.1:8442"\ndata = "data"\n\n[turnstile]\n# Seconds to wait for compute to wake.\nwait = 180\n')
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"turnstile": {"wait": 60, "host": "0.0.0.0"}, "weightroom": {}}))
    settings = turnstile.load_settings(front)
    assert (settings.wait, settings.host) == (60, "127.0.0.1")  # where it listens is not changed from the site
    turnstile.say_what_runs(front, settings)
    left = json.loads((tmp_path / "data" / "settings-seen-turnstile.json").read_text())
    assert left["wait"] == {"value": 60, "says": "Seconds to wait for compute to wake.", "file": 180} and left["ports"]["value"] == [8442, 8443, 8444]
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"turnstile": {"wait": 0}}))
    assert turnstile.load_settings(front).wait == 180  # a change it cannot use: the file's value holds


def test_image_preview_policy_keeps_the_file_default_separate_from_live_overrides(tmp_path):
    front=tmp_path/'weightroom.toml'
    front.write_text('compute = "http://compute:8442"\ndata = "data"\n[turnstile]\nimage_adult_previews = true\n')
    (tmp_path/'data').mkdir()
    (tmp_path/'data/settings.json').write_text(json.dumps({'turnstile':{'image_adult_previews':False}}))
    # Resetting a saved override must return to the file's value, including after restart.
    settings=turnstile.load_settings(front)
    assert settings.image_adult_previews is True
    turnstile.say_what_runs(front,settings)
    assert json.loads((tmp_path/'data/settings-seen-turnstile.json').read_text())['image_adult_previews']['value'] is True
    front.write_text('compute = "http://compute:8442"\n[turnstile]\nimage_adult_previews = "true"\n')
    with pytest.raises(SystemExit,match='image_adult_previews must be true or false'):
        turnstile.load_settings(front)


def test_a_program_that_reaches_loadcoach_over_the_network_keeps_in_step_and_keeps_a_copy(tmp_path):
    file = tmp_path / "voice.toml"
    file.write_text('# Seconds a silent call is kept.\nidle_seconds = 120\n\nmodel_parallel = 1\n\n[profiles.default]\nmodel = "small"\n')
    settings, following = with_changes("voice", file)
    assert settings == {"idle_seconds": 120, "model_parallel": 1, "profiles": {"default": {"model": "small"}}} and "profiles" in CHANGEABLE["voice"]
    asked, changed = [], {"idle_seconds": 30, "model_parallel": 2, "profiles": "none", "port": 1}

    def loadcoach(request):  # LoadCoach, as far as this program sees it
        asked.append((request.method, request.url.path, json.loads(request.content or b"null"), request.headers["authorization"]))
        return httpx.Response(200, json=changed if request.method == "GET" else {})

    async def for_a_moment():
        try:
            await asyncio.wait_for(following(httpx.AsyncClient(transport=httpx.MockTransport(loadcoach)), "http://compute", "its-own-key"), 0.2)
        except TimeoutError:
            pass

    asyncio.run(for_a_moment())
    # It reads its settings where it uses them, so a change is simply there. One of the wrong kind is refused, one that may not be changed passed over.
    assert settings["idle_seconds"] == 30 and settings["profiles"] == {"default": {"model": "small"}} and "port" not in settings
    told = next(body for method, path, body, _ in asked if method == "POST")
    assert told["refused"] == {"profiles": "profiles must be a table."} and told["told"]["idle_seconds"] == {"value": 30, "says": "Seconds a silent call is kept.", "file": 120}
    assert told["told"]["model_parallel"]["value"] == 1  # this one holds from its next start, and it says what is in force now
    assert [(path, key) for method, path, _, key in asked if method == "POST"] == [("/loadcoach/settings/voice/seen", "Bearer its-own-key")]

    # With compute off it starts with the copy it kept.
    again, _ = with_changes("voice", file)
    assert again["idle_seconds"] == 30 and again["model_parallel"] == 2


def test_a_program_reads_its_defaults_under_its_own_file_and_keeps_what_it_writes_in_a_data_folder(tmp_path):
    from loadcoach.config import load_config, read_settings

    folder = tmp_path / "settings"
    (folder / "defaults").mkdir(parents=True)
    (folder / "defaults" / "loadcoach.toml").write_text('# How many requests are kept in full.\nkeep_text = 50\n\nmax_wait = 600\n')
    (folder / "loadcoach.toml").write_text('# Where it listens.\nhost = "10.0.0.5"\ndata = "../data"\nmax_wait = 30\n')
    (tmp_path / "data").mkdir()
    config = load_config(folder / "loadcoach.toml")
    # The defaults give what the machine's file leaves unsaid. What that file does say is the machine's own, and wins.
    assert (config.host, config.keep_text, config.max_wait) == ("10.0.0.5", 50, 30)
    assert config.database == str(folder / "../data/loadcoach.db") and config.token_file == str(folder / "../data/loadcoach.cli-token")
    assert config.says["keep_text"] == "How many requests are kept in full." and config.says["host"] == "Where it listens."
    # A change from the site lies over both, and the other programs find LoadCoach's database the same way.
    Changes(config.database).put("loadcoach", "keep_text", 7, "jordan")
    assert load_config(folder / "loadcoach.toml").keep_text == 7 and (tmp_path / "data" / "loadcoach.db").exists()
    (folder / "spotter.toml").write_text('data = "../data"\n[services]\nloadcoach = "loadcoach.service"\n')
    watching = spotter.load_settings(folder / "spotter.toml")
    assert watching.database == config.database and watching.history == str(folder / "../data/spotter.db")

    # A table in both files has its keys put together: turnstile's part of the control node's file.
    (folder / "defaults" / "weightroom.toml").write_text('[turnstile]\nwait = 180\n')
    (folder / "weightroom.toml").write_text('compute = "http://10.0.0.1:8442"\ndata = "../data"\n[turnstile]\nhost = "0.0.0.0"\n')
    assert read_settings(folder / "weightroom.toml")["turnstile"] == {"wait": 180, "host": "0.0.0.0"}
    assert (turnstile.load_settings(folder / "weightroom.toml").wait, turnstile.load_settings(folder / "weightroom.toml").host) == (180, "0.0.0.0")


@pytest.mark.parametrize("program", ["spotter", "turnstile"])
def test_component_check_interval_is_configurable_and_validated(tmp_path, program):
    from weightroom.setting_labels import sections
    (tmp_path / "loadcoach.toml").write_text("")
    path = tmp_path / ("spotter.toml" if program == "spotter" else "weightroom.toml")
    prefix = "" if program == "spotter" else 'compute = "http://compute:8442"\n[turnstile]\n'
    module = spotter if program == "spotter" else turnstile
    path.write_text(prefix + "component_versions_every = 5\n")
    initial = module.load_settings(path)
    assert initial.component_versions_every == 5
    if program == "spotter":
        live, refused = taken(program, initial, {"component_versions_every": 2}, spotter.settings_from)
        assert live.component_versions_every == 2 and not refused
        refused_settings, refused = taken(program, live, {"component_versions_every": 0}, spotter.settings_from)
        assert refused_settings is live and "component_versions_every" in refused
        reset, refused = taken(program, live, {}, spotter.settings_from)
        assert reset.component_versions_every == 5 and not refused
    assert CHANGEABLE[program]["component_versions_every"] == "now"
    for value in ("0", "-1", "true", '"five"', "nan", "inf"):
        path.write_text(prefix + f"component_versions_every = {value}\n")
        with pytest.raises(SystemExit, match="component_versions_every"):
            module.load_settings(path)
    setting = {"name": "component_versions_every", "value": 5, "when": "now"}
    grouped = sections([{"program": program, "settings": [setting]}], compute_node=(program == "spotter"))
    assert grouped[0]["title"] == ("Telemetry, power and idle" if program == "spotter" else "Alerts and monitoring")
