"""Deploying the suite from front: front's source checkout, the machines it goes to, and the jobs.

Front keeps a git checkout of the suite (`[deploy] source`) and pulls it from GitHub. A deploy
runs scripts/deploy-node.sh from that checkout as a transient systemd unit, so it outlives the
weightroom restart it may cause. Its output goes to a log file in data/deploys, which is the job.

    [deploy]
    source = "~/openWeightGym-source"   # front's checkout of the suite
    branch = "main"
    [deploy.nodes.front]
    ssh = "local"               # how front reaches it; "local" is front itself
    folder = "weightroom"       # the code's folder there, under the home folder
    extras = "voice-gateway,benchmarks,postgresql,knowledge"
    services = ["turnstile", "voice-gateway", "weightroom"]   # restarted in this order
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NAME = re.compile(r"[a-z0-9_-]{1,40}")
UNIT = "suite-deploy-"
SETUP_SERVICES = ("loadcoach", "spotter", "toollocker", "voice")  # what scripts/install-suite.sh can set up on a compute machine, in restart order


def checked(deploy: dict) -> dict:
    """The [deploy] part with its nodes validated, so nothing from the file reaches a shell unchecked."""
    nodes = {}
    for name, node in deploy.get("nodes", {}).items():
        services = node.get("services", [])
        if (not NAME.fullmatch(name) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", str(node.get("ssh", "")))
                or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_/-]*", str(node.get("folder", ""))) or ".." in node["folder"]
                or not re.fullmatch(r"[a-z,-]*", str(node.get("extras", "")))
                or not isinstance(services, list) or not all(isinstance(one, str) and NAME.fullmatch(one) for one in services)):
            raise ValueError(f"[deploy.nodes.{name}] needs ssh, a relative folder, extras and a list of service names")
        nodes[name] = {"ssh": node["ssh"], "folder": node["folder"], "extras": node.get("extras", ""), "services": services}
    return {"source": str(Path(deploy.get("source", "~/openWeightGym-source")).expanduser()), "branch": deploy.get("branch", "main"), "nodes": nodes}


def add_node(path: Path, name: str, ssh: str, folder: str, services: list[str]) -> None:
    """Append a [deploy.nodes.NAME] part to the settings file, so the new machine is configured where the others are."""
    services = [one for one in SETUP_SERVICES if one in services]
    if not services:
        raise ValueError("Choose at least one service to set up.")
    node = {"ssh": ssh, "folder": folder, "extras": "voice" if "voice" in services else "", "services": services}
    checked({"nodes": {name: node}})  # the same checks as the file's own nodes, so the values below need no quoting
    text = path.read_text()
    if name in tomllib.loads(text).get("deploy", {}).get("nodes", {}):
        raise ValueError(f"There is already a machine named {name}.")
    text = text.rstrip("\n") + f'\n\n[deploy.nodes.{name}]\nssh = "{ssh}"\nfolder = "{folder}"\nextras = "{node["extras"]}"\nservices = {json.dumps(services)}\n'
    tomllib.loads(text)
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    os.replace(part, path)


def git(source: str, *args: str, timeout: float = 10) -> str:
    """One git command in the source checkout; its output, or an empty string if it failed."""
    done = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True, timeout=timeout)
    return done.stdout.strip() if done.returncode == 0 else ""


def source_state(deploy: dict) -> dict:
    """What front's checkout holds, and how far behind the branch on GitHub it was at the last fetch."""
    source, branch = deploy["source"], deploy["branch"]
    if not Path(source, ".git").is_dir():
        return {"missing": True, "source": source}
    head = git(source, "log", "-1", "--format=%H%x00%h%x00%s%x00%ci")
    full, short, subject, when = (head.split("\0") + ["", "", "", ""])[:4]
    return {"source": source, "branch": branch, "commit": full, "short": short, "subject": subject, "when": when,
            "behind": git(source, "rev-list", "--count", f"HEAD..origin/{branch}") or "?",
            "coming": git(source, "log", "--format=%h %s", f"HEAD..origin/{branch}").splitlines()[:20]}


def pull(deploy: dict) -> str:
    """Fetch the branch from GitHub and move the checkout to it. Never merges: only a fast-forward."""
    source, branch = deploy["source"], deploy["branch"]
    for args in (["fetch", "-q", "origin", branch], ["merge", "-q", "--ff-only", f"origin/{branch}"]):
        done = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True, timeout=120)
        if done.returncode:
            return f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}"
    return ""


def node_state(node: dict) -> dict:
    """The commit deployed on one machine and whether each of its services is running, asked over ssh."""
    command = f"cat {node['folder']}/DEPLOYED 2>/dev/null; echo ---; systemctl --user is-active {' '.join(s + '.service' for s in node['services'])}"
    argv = ["bash", "-c", "cd ~ && " + command] if node["ssh"] == "local" else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", node["ssh"], command]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"reachable": False, "why": "did not answer within 10 seconds"}
    if "---" not in done.stdout:
        return {"reachable": False, "why": (done.stderr.strip().splitlines() or ["no answer"])[-1]}
    deployed, states = done.stdout.split("---", 1)
    return {"reachable": True, "deployed": deployed.strip(), "services": dict(zip(node["services"], states.split()))}


def nodes_state(deploy: dict) -> dict:
    """Every node's state, asked at once."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(deploy["nodes"], pool.map(node_state, deploy["nodes"].values())))


def running() -> bool:
    """True while a deploy or restart started from this page is still going."""
    done = subprocess.run(["systemctl", "--user", "list-units", UNIT + "*", "--state=active", "--no-legend", "--plain"],
                          capture_output=True, text=True, timeout=10)
    return bool(done.stdout.strip())


def start(deploy: dict, logs: Path, name: str, action: str, by: str) -> str:
    """Start a deploy or restart of one node as a transient unit, and return the job's name."""
    node = deploy["nodes"][name]
    job = f"{time.strftime('%Y%m%d-%H%M%S')}-{action}-{name}"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{job}.log"
    log.write_text(f"{action} {name} by {by} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if action == "setup":
        if not node["services"] or set(node["services"]) - set(SETUP_SERVICES):
            raise ValueError(f"Setup can install {', '.join(SETUP_SERVICES)}; {name} lists others. Install it by hand.")
        args = ["--setup", node["ssh"], node["folder"], ",".join(node["services"])]
    elif action == "restart":
        args = ["--restart", node["ssh"], *node["services"]]
    else:
        args = [node["ssh"], node["folder"], node["extras"] or "-", *node["services"]]
    script = f"scripts/deploy-node.sh {shlex.join(args)} >> {shlex.quote(str(log))} 2>&1; echo \"exit=$?\" >> {shlex.quote(str(log))}"
    subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={UNIT}{job}", f"--working-directory={deploy['source']}",
                    "bash", "-c", script], check=True, capture_output=True, timeout=10)
    return job


def jobs(logs: Path, limit: int = 10) -> list[dict]:
    """The latest jobs, newest first, each with whether it finished and how."""
    found = []
    for log in sorted(logs.glob("*.log"), reverse=True)[:limit]:
        last = log.read_text(errors="replace").rstrip().rsplit("\n", 1)[-1]
        found.append({"job": log.stem, "done": last.startswith("exit="), "ok": last == "exit=0"})
    return found


def read_job(logs: Path, job: str) -> str:
    """One job's output, at most its last 400 lines."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-(deploy|restart|setup)-[a-z0-9_-]+", job) or not (logs / f"{job}.log").is_file():
        raise ValueError("There is no such deploy job.")
    return "\n".join((logs / f"{job}.log").read_text(errors="replace").splitlines()[-400:])
