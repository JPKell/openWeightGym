"""The remote tool host installer, scripts/install-tool-host.sh (milestones 73 and 74).

It runs for real against a home folder of the test's own. A stand-in takes systemctl's place, so
no service is touched; the unit it writes is then run by hand, as systemd would run it.
"""

import os
import shlex
import socket
import subprocess
from pathlib import Path

import httpx

SCRIPT = Path(__file__).parents[1] / "scripts" / "install-tool-host.sh"


def install(home, *arguments):
    environment = {**os.environ, "HOME": str(home), "SYSTEMCTL": "true", "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": ""}
    return subprocess.run([str(SCRIPT), *arguments], env=environment, capture_output=True, text=True, timeout=60)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serving(home, port):
    """Run the unit's command as systemd would, in its folder. Returns the process, once it answers."""
    unit = (home / ".config" / "systemd" / "user" / "toollocker-host.service").read_text()
    command = shlex.split(next(line for line in unit.splitlines() if line.startswith("ExecStart=")).removeprefix("ExecStart="))
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    server = subprocess.Popen(command, cwd=home / ".local" / "share" / "toollocker-host", env=environment, stderr=subprocess.PIPE, text=True)
    for line in server.stderr:
        if "answering MCP over HTTP" in line:
            return server
    raise AssertionError("the tool host did not start")


def ask(port, key, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return httpx.post(f"http://127.0.0.1:{port}/mcp", json=body, headers={"authorization": f"Bearer {key}"} if key else {})


def test_it_sets_up_one_service_for_a_tool_folder_and_says_what_to_put_on_compute(tmp_path):
    home, shared, port = tmp_path / "home", tmp_path / "shared files", free_port()
    home.mkdir()
    shared.mkdir()
    (shared / "note.txt").write_text("hello\n")
    done = install(home, "--name", "nas", "--description", "the NAS", "--files", str(shared), "--port", str(port), "--bind", "127.0.0.1", "--compute", "10.0.0.5")
    assert done.returncode == 0, done.stderr

    prefix, config = home / ".local" / "share" / "toollocker-host", home / ".config" / "toollocker-host"
    key = config / "host.key"
    assert key.stat().st_mode & 0o777 == 0o600 and key.read_text().strip() in done.stdout
    assert (config / "tools.toml").stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in (prefix / "tools").iterdir()) == ["code.py", "files.py"] and (prefix / "README.md").exists()
    settings = (config / "tools.toml").read_text()
    assert f'[files]\nfolders = ["{shared}"]' in settings and "# [code]" in settings  # code is written ready to fill in
    assert '[servers.nas]\nurl = "http://' in done.stdout and f':{port}/mcp"\nkey_file = "../keys/nas.key"\ndescription = "the NAS"\nlevel = "normal"' in done.stdout
    assert f"sudo ufw allow from 10.0.0.5 to any port {port} proto tcp" in done.stdout
    unit = (home / ".config" / "systemd" / "user" / "toollocker-host.service").read_text()
    assert "--bind 127.0.0.1" in unit and "NoNewPrivileges=true" in unit and "MemoryMax=512M" in unit
    assert (home / ".config" / "systemd" / "user" / "toollocker-host.service").stat().st_mode & 0o777 == 0o600

    # The service answers with the key and only with it, with the tools of every file set up.
    server = serving(home, port)
    try:
        secret = key.read_text().strip()
        assert [tool["name"] for tool in ask(port, secret, "tools/list").json()["result"]["tools"]] == ["list_folder", "read_file"]  # code has no section
        read = ask(port, secret, "tools/call", {"name": "read_file", "arguments": {"file": str(shared / "note.txt")}})
        assert read.json()["result"]["content"][0]["text"] == "1\thello\n" and ask(port, "", "tools/list").status_code == 401
    finally:
        server.kill()
        server.wait()

    # A tool file of one's own is kept when the installer runs again, and served from the next start.
    (prefix / "tools" / "clock.py").write_text('from toollocker.tools import tool\n@tool\ndef tick() -> str:\n    """Say tick."""\n    return "tick"\n')
    before = key.read_text()
    again = install(home, "--name", "nas", "--port", str(port))
    assert again.returncode == 0 and key.read_text() == before and (prefix / "tools" / "clock.py").exists()
    server = serving(home, port)
    try:
        assert "tick" in [tool["name"] for tool in ask(port, before.strip(), "tools/list").json()["result"]["tools"]]
    finally:
        server.kill()
        server.wait()

    # Once the settings exist, folders are changed there, not by running it again.
    refused = install(home, "--name", "nas", "--files", str(shared))
    assert refused.returncode == 1 and "Edit" in refused.stderr and (config / "tools.toml").read_text() == settings


def test_it_changes_nothing_when_asked_for_something_it_cannot_do(tmp_path):
    for wrong, says in ((["--name", "NAS", "--files", str(tmp_path)], "--name is lowercase"),
                        (["--name", "nas", "--files", str(tmp_path / "missing")], "not a folder"),
                        (["--name", "nas", "--files", str(tmp_path), "--port", "99999"], "a port is a number"),
                        (["--name", "nas", "--bind", "bad-address"], "--bind must be an IP address")):  # fmt: skip
        done = install(tmp_path, *wrong)
        assert done.returncode == 1 and says in done.stderr
    assert not (tmp_path / ".config").exists() and not (tmp_path / ".local").exists()
