"""Which of the ToolLocker's tools a group and an account are shown, as LoadCoach keeps it (milestone 73).

The ToolLocker's own checks, that a tool not shown is neither listed nor called, are in
test_toollocker.py. These are about the lists themselves: reading, adjusting, and who may hand
them out.
"""

import pytest

from loadcoach.groups import Group, adjusted, beyond, check_own, limited, read_group, section_of
from test_app import add_user, fake_llama_server, gateway, in_an_empty_folder  # noqa: F401  fixtures, used by name

NAS = read_group("nas", {"tools": ["nas__*", "code__search"]})


def test_a_group_lists_tools_by_name_or_pattern_and_star_is_every_tool():
    assert NAS.may_see_tool("nas__read_file") and NAS.may_see_tool("code__search") and not NAS.may_see_tool("code__diff")
    assert Group().may_see_tool("anything") and read_group("all", {"tools": "*"}).tools is None
    assert section_of(NAS)["tools"] == ["nas__*", "code__search"] and section_of(Group())["tools"] == "*"
    for wrong in ({"tools": "nas__*"}, {"tools": [1]}, {"tools": [""]}):
        with pytest.raises(ValueError):
            read_group("bad", wrong)


def test_an_account_is_shown_more_or_fewer_tools_than_its_group():
    mine = adjusted(NAS, {"add_tools": ["code__diff"], "remove_tools": ["nas__private*"]})
    assert mine.may_see_tool("code__diff") and mine.may_see_tool("nas__read_file") and not mine.may_see_tool("nas__private_notes")
    # A group shown every tool has nothing to add, but can still have one taken away.
    assert adjusted(Group(), {"add_tools": ["x"]}).tools is None and not adjusted(Group(), {"remove_tools": ["x"]}).may_see_tool("x")
    for wrong in ({"add_tools": "x"}, {"remove_tools": [1]}):
        with pytest.raises(ValueError):
            check_own(wrong)
    # Through an app, only what both the person and the app are shown.
    through = limited(mine, read_group("app", {"tools": ["nas__read_file", "office__*"]}))
    assert through.tools == ("nas__read_file",) and through.hidden_tools == ("nas__private*",)


def test_nobody_hands_out_tools_they_are_not_shown():
    assert beyond(Group(), NAS) is None  # shown everything, one may hand out anything
    assert beyond(NAS, read_group("w", {"tools": ["nas__read_file"]})) is None
    assert beyond(NAS, Group()) == "tools you may not see yourself"
    assert beyond(NAS, read_group("w", {"tools": ["*"]})) == "tools you may not see yourself"
    hidden = adjusted(NAS, {"remove_tools": ["nas__secret"]})
    assert beyond(hidden, read_group("w", {"tools": ["nas__*"]})) == "tools you may not see yourself"  # it would show nas__secret
    assert beyond(hidden, read_group("w", {"tools": ["nas__read_file"]})) is None


def test_tools_are_kept_with_groups_and_accounts_and_said_by_me(fake_llama_server):  # noqa: F811
    with gateway(fake_llama_server) as caller:
        admin = add_user(caller, "sam", "admin")
        made = caller.post("/loadcoach/groups/keepers", json={"can": ["manage-users", "manage-groups"], "tools": ["nas__*"]}, headers=admin)
        changed = caller.patch("/loadcoach/groups/keepers", json={"tools": ["nas__*", "code__search"]}, headers=admin)
        listed = {group["name"]: group for group in caller.get("/loadcoach/groups", headers=admin).json()["groups"]}
        keeper = add_user(caller, "kim", "keepers", by=admin)
        pat = add_user(caller, "pat", "keepers", by=admin)
        own = caller.post("/loadcoach/users/pat", json={"own": {"add_tools": ["code__diff"], "remove_tools": ["nas__secret"]}}, headers=admin)
        me = caller.get("/loadcoach/me", headers=pat).json()
        # Kim is shown only nas__* and code__search, so may not make a group shown more, nor give an account more.
        wider = caller.post("/loadcoach/groups/wider", json={"tools": "*"}, headers=keeper)
        narrower = caller.post("/loadcoach/groups/narrower", json={"tools": ["nas__read_file"]}, headers=keeper)
        more = caller.post("/loadcoach/users/pat", json={"own": {"add_tools": ["office__*"]}}, headers=keeper)

    assert (made.status_code, changed.status_code, own.status_code) == (201, 200, 200)
    assert listed["keepers"]["tools"] == ["nas__*", "code__search"] and listed["admin"]["tools"] == "*"
    assert (me["tools"], me["hidden_tools"]) == (["nas__*", "code__search", "code__diff"], ["nas__secret"])
    assert wider.status_code == 403 and "tools you may not see yourself" in wider.json()["error"]["message"]
    assert narrower.status_code == 201 and more.status_code == 403
