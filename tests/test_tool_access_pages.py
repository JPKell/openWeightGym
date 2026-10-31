"""The site's side of milestone 73: tool levels and tool sets on System → Tools, and the tools a group and an account are shown."""

import json

from loadcoach.log import Log
from loadcoach.settings import Changes, said
from test_weightroom import desk, sign_in  # noqa: F401  the fixture, used by name


def test_tool_levels_and_tool_sets_are_changed_on_the_tools_page(desk, tmp_path):  # noqa: F811
    browser, switch, _ = desk
    changes = switch.front.state.front_settings.changes
    database = changes.db.database
    config = {"call_timeout": 30, "servers": {"nas": {"url": "http://nas:9100/mcp", "key_file": "k", "level": "sensitive", "description": "the NAS"}},
              "toolsets": {"reading": {"tools": ["nas__*"]}}, "tool_levels": {}}  # fmt: skip
    changes.seen("toollocker", said(config, {}, {}))
    log = Log(database)
    log.db.execute("CREATE TABLE tool_schemas (tool TEXT PRIMARY KEY, schema TEXT NOT NULL)")
    for name in ("nas__read_file", "nas__list_folder"):
        log.db.execute("INSERT INTO tool_schemas VALUES (?, ?)", (name, json.dumps({"name": name})))
    sign_in(browser, "jordan")

    page = browser.get("/tools").text
    assert "the NAS" in page and "http://nas:9100/mcp" in page and "as its server (sensitive)" in page and 'name="set:reading"' in page

    saved = browser.post("/tools", data={"do": "levels", "level:nas__read_file": "normal", "level:nas__list_folder": ""})
    assert "Saved." in saved.text and Changes(database).of("toollocker")["tool_levels"] == {"nas__read_file": "normal"}

    browser.post("/tools", data={"do": "toolsets", "set:reading": "nas__read_file\nnas__list_folder", "new_set": "code", "new_tools": "code__*, files__read_file"})
    assert Changes(database).of("toollocker")["toolsets"] == {"reading": {"tools": ["nas__read_file", "nas__list_folder"]}, "code": {"tools": ["code__*", "files__read_file"]}}
    browser.post("/tools", data={"do": "toolsets", "set:reading": "nas__*", "set:code": "code__*", "drop:code": "on"})
    assert Changes(database).of("toollocker")["toolsets"] == {"reading": {"tools": ["nas__*"]}}

    bad_name = browser.post("/tools", data={"do": "toolsets", "set:reading": "nas__*", "new_set": "Bad Name", "new_tools": "x"})
    empty = browser.post("/tools", data={"do": "toolsets", "set:reading": ""})
    assert bad_name.status_code == empty.status_code == 400 and "needs at least one tool" in empty.text
    sign_in(browser, "pat")
    assert browser.post("/tools", data={"do": "levels", "level:nas__read_file": "normal"}).status_code == 403


def test_a_group_and_an_account_are_shown_the_tools_typed_on_their_pages(desk):  # noqa: F811
    browser, _, users = desk
    sign_in(browser, "jordan")
    browser.post("/groups", data={"name": "readers"})
    page = browser.get("/groups/readers").text
    assert ">Tools members are shown</h2>" in page and 'id="every_tool" name="every_tool" type="checkbox" class="form-check-input" checked' in page

    browser.post("/groups/readers", data={"do": "tools", "tools": "nas__*\ncode__search, files__read_file"})
    assert users.groups.stored("readers")["tools"] == ["nas__*", "code__search", "files__read_file"]
    browser.post("/groups/readers", data={"do": "tools", "tools": "nas__*", "every_tool": "on"})
    assert users.groups.stored("readers")["tools"] == "*"

    assert ">Tools of its own<" in browser.get("/users/pat").text and "Its group is shown every tool" in browser.get("/users/pat").text
    browser.post("/users/pat", data={"do": "tools", "add_tools": "", "remove_tools": "code__diff\nnas__secret"})
    assert users.own("pat") == {"remove_tools": ["code__diff", "nas__secret"]}
    browser.post("/users/pat", data={"do": "limits", "priority": "high"})  # the limits form keeps the tools of its own
    assert users.own("pat") == {"remove_tools": ["code__diff", "nas__secret"], "priority": "high"}
