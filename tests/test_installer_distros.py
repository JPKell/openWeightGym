"""The installer across Linux distributions: package managers that never ask, PostgreSQL made the
way each family expects, and failures that stop loudly and say what to do next."""
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def one_function(name: str, function: str) -> str:
    """Extract one real function (a one-line one too) from a script with top-level setup."""
    text = (ROOT / name).read_text()
    found = (re.search(rf"(?m)^{re.escape(function)}\(\) \{{ .*\}}$", text)
             or re.search(rf"(?ms)^{re.escape(function)}\(\) \{{.*?^\}}", text))
    assert found, function
    return found[0]


def pg_instead() -> str:
    """The PostgreSQL fallback steps install-suite.sh lists on every PostgreSQL failure."""
    found = re.search(r"(?ms)^PG_INSTEAD=\(.*?\)$", (ROOT / "scripts/install-suite.sh").read_text())
    assert found
    return found[0]


def bash(tmp_path, source: str, *, env=None, stdin=None):
    """Run a fixture-only shell with the installer's libraries, no terminal and a private home."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {**os.environ, "OWG_NO_TTY": "1", "OWG_PLAIN": "1", "HOME": str(home),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
                   "OWG_LOG": str(tmp_path / "install.log"), "TEST_REPO": str(ROOT), **(env or {})}
    return subprocess.run(["bash", "-c", 'source "$TEST_REPO/scripts/lib/ui.sh"\nsource "$TEST_REPO/scripts/lib/detect.sh"\n'
                           "ui_init\nui_log_init\n" + source],
                          cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=30,
                          input=stdin)


def fake_programs(tmp_path, *names, script="#!/bin/sh\nexit 0\n") -> str:
    """A folder of stand-in programs, put first on PATH."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_text(script)
        (folder / name).chmod(0o755)
    return f"{folder}{os.pathsep}{os.environ['PATH']}"


@pytest.mark.parametrize("family,tool,expected,typed", [
    ("arch", "pacman", "pacman -S --needed --noconfirm", "sudo pacman -S --needed postgresql"),
    ("debian", "apt-get", "env DEBIAN_FRONTEND=noninteractive apt-get install -y", "sudo apt-get install postgresql"),
    ("fedora", "dnf", "dnf install -y", "sudo dnf install postgresql"),
    ("suse", "zypper", "zypper --non-interactive install", "sudo zypper install postgresql"),
])
def test_package_managers_are_told_never_to_ask(tmp_path, family, tool, expected, typed):
    answer = bash(tmp_path, f'OS_FAMILY={family}\ndetect_package_manager\n'
                            'printf "%s\\n" "${PKG_INSTALL[*]}" "$(pkg_command postgresql)"',
                  env={"PATH": fake_programs(tmp_path, tool)})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.splitlines() == [expected, typed]


def test_os_release_names_opensuse_as_its_own_family(tmp_path):
    release = tmp_path / "os-release"
    release.write_text('NAME="openSUSE Leap"\nID="opensuse-leap"\nID_LIKE="suse opensuse"\nVERSION_ID="16.0"\n')
    script = one_function("scripts/lib/detect.sh", "detect_os").replace("/etc/os-release", str(release))
    answer = bash(tmp_path, script + '\ndetect_os\necho "$OS_FAMILY"')
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip() == "suse"


def test_a_stop_is_loud_with_the_log_and_numbered_next_steps(tmp_path):
    answer = bash(tmp_path, '''
OWG_RETRY="Run the installer again: ~/openWeightGym/install.sh" OWG_STOP_TITLE="The install stopped"
printf 'error: failed to commit transaction (conflicting files)\\n' >>"$OWG_LOG"
ui_die "PostgreSQL could not be installed with pacman." "Bring the system up to date: sudo pacman -Syu" \\
  "Or keep the data in SQLite"''')
    assert answer.returncode == 78
    said = answer.stderr
    assert "The install stopped" in said
    assert "PostgreSQL could not be installed with pacman." in said
    assert "failed to commit transaction" in said
    assert "1. Bring the system up to date: sudo pacman -Syu" in said
    assert "2. Or keep the data in SQLite" in said
    assert "3. Run the installer again: ~/openWeightGym/install.sh" in said
    assert f"Full log: {tmp_path / 'install.log'}" in said


def test_a_child_that_already_said_why_is_not_said_again(tmp_path):
    quiet = bash(tmp_path, 'ui_child_failed 78 "Setting up did not finish."')
    assert quiet.returncode == 78 and quiet.stderr == ""
    loud = bash(tmp_path, 'ui_child_failed 1 "Setting up did not finish." "See the log"')
    assert loud.returncode == 78 and "Setting up did not finish." in loud.stderr and "1. See the log" in loud.stderr


def test_attention_items_are_gathered_into_one_panel_at_the_end(tmp_path):
    answer = bash(tmp_path, '''
ui_attention "loadcoach is not running" "See why: owg logs loadcoach" "Put that right, then start it: owg restart loadcoach"
ui_attention "The services stop when you log out of this machine" "Let them keep running: sudo loginctl enable-linger fixture"
ui_attention_box''')
    assert answer.returncode == 0, answer.stderr
    said = answer.stdout + answer.stderr
    assert "Needs your attention (2)" in said
    assert "1. See why: owg logs loadcoach" in said and "2. Put that right, then start it" in said
    assert "1. Let them keep running: sudo loginctl enable-linger fixture" in said


def test_units_of_an_older_installation_drop_ins_and_masks_are_all_found(tmp_path):
    units = tmp_path / "home/.config/systemd/user"
    (units / "turnstile.service.d").mkdir(parents=True)
    (units / "loadcoach.service").write_text("[Service]\nExecStart=%h/ai/suite/.venv/bin/loadcoach serve\n")
    (units / "weightroom.service").write_text(f"[Service]\nExecStart={tmp_path}/home/openWeightGym/.venv/bin/weightroom\n")
    (units / "turnstile.service.d/override.conf").write_text("[Service]\nEnvironment=PYTHONPATH=/elsewhere\n")
    (units / "spotter.service").symlink_to("/dev/null")
    answer = bash(tmp_path, '''
detect_units "$HOME/openWeightGym"
units_retire_steps
printf '%s\\n' "other=${UNITS_OTHER[*]}" "root=$UNITS_OTHER_ROOT" "masked=${UNITS_MASKED[*]}" "dropins=${UNITS_DROPINS[*]}"
printf 'step: %s\\n' "${UNITS_STEPS[@]}"''')
    assert answer.returncode == 0, answer.stderr
    said = answer.stdout
    assert "other=loadcoach\n" in said
    assert f"root={tmp_path}/home/ai/suite\n" in said
    assert "masked=spotter\n" in said
    assert f"dropins={units}/turnstile.service.d/override.conf\n" in said
    assert "step: Stop its services: systemctl --user disable --now loadcoach.service" in said
    assert ("mkdir -p ~/.config/systemd/retired-units && mv ~/.config/systemd/user/loadcoach.service "
            "~/.config/systemd/user/turnstile.service.d ~/.config/systemd/retired-units/") in said
    assert "step: Make systemd forget them: systemctl --user daemon-reload" in said


def test_an_older_installation_stops_the_install_with_steps_that_leave_its_data_alone(tmp_path):
    units = tmp_path / "home/.config/systemd/user"
    units.mkdir(parents=True)
    (units / "loadcoach.service").write_text("[Service]\nExecStart=%h/ai/suite/.venv/bin/loadcoach serve\n")
    answer = bash(tmp_path, 'DIR=$HOME/openWeightGym\n' + one_function("install.sh", "check_existing_units")
                  + "\ncheck_existing_units")
    assert answer.returncode == 78
    said = answer.stderr
    assert "Another openWeightGym (or AiSuite) installation" in said and "~/ai/suite" in said
    assert "systemctl --user disable --now loadcoach.service" in said
    assert "is not touched by any of this" in said


POSTGRES_SUDO = r'''
sudo() {
  [[ $1 == -n ]] && shift
  printf 'sudo %s\n' "$*" >>"$CALLS"
  if read -r -t 0.2 line; then printf 'stdin: %s\n' "$line" >>"$CALLS"; fi
  case "$1" in
    test) return 1 ;;
    -u) [[ $3 == psql ]] && return 0; return 0 ;;
    pacman|apt-get|dnf|zypper|env) return "${PACKAGE_STATUS:-0}" ;;
  esac
  return 0
}
'''


def postgresql_fixture(manager: str) -> str:
    """install-suite.sh's own PostgreSQL functions, for one package manager and a stand-in sudo."""
    install = {"pacman": "pacman -S --needed --noconfirm", "apt": "env DEBIAN_FRONTEND=noninteractive apt-get install -y",
               "dnf": "dnf install -y", "zypper": "zypper --non-interactive install"}[manager]
    return (f"PKG_MANAGER={manager}\nPKG_INSTALL=({install})\nOS_NAME=Fixture\nOWG_RETRY_COMMAND=install.sh\n"
            "INSTALL_POSTGRESQL=1 PROVISION_POSTGRESQL=1 DRY_RUN=0\nui_sudo() { :; }\ndie() { ui_die \"$@\"; }\n"
            + pg_instead() + "\n" + POSTGRES_SUDO + "\n".join(
                one_function("scripts/install-suite.sh", name) for name in
                ("try_step", "as_postgres", "postgresql_packages", "initdb_program", "install_postgresql_packages",
                 "start_postgresql", "wait_for_postgresql")))


systemd_here = pytest.mark.skipif(not Path("/run/systemd/system").is_dir(), reason="start_postgresql needs systemd")


@systemd_here
@pytest.mark.parametrize("manager,data", [("pacman", "/var/lib/postgres/data"), ("zypper", "/var/lib/pgsql/data")])
def test_arch_and_opensuse_get_a_cluster_with_password_logins_over_tcp(tmp_path, manager, data):
    calls = tmp_path / "calls"
    answer = bash(tmp_path, postgresql_fixture(manager) + "\ninstall_postgresql_packages",
                  env={"CALLS": str(calls), "PATH": fake_programs(tmp_path, "initdb")}, stdin="y\ny\ny\n")
    assert answer.returncode == 0, answer.stdout + answer.stderr
    made = calls.read_text()
    assert "stdin:" not in made, "a package manager could read the piped installer"
    assert f"sudo -u postgres {tmp_path}/bin/initdb -D {data} --auth-local=peer --auth-host=scram-sha-256" in made
    assert "sudo systemctl enable --now postgresql.service" in made
    if manager == "pacman":
        assert "sudo pacman -S --needed --noconfirm postgresql\n" in made


@systemd_here
def test_fedora_and_rhel_create_their_cluster_with_postgresql_setup(tmp_path):
    calls = tmp_path / "calls"
    answer = bash(tmp_path, postgresql_fixture("dnf") + "\ninstall_postgresql_packages",
                  env={"CALLS": str(calls), "PATH": fake_programs(tmp_path, "postgresql-setup")})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    made = calls.read_text()
    assert "sudo dnf install -y postgresql-server postgresql\n" in made
    assert ("sudo env PGSETUP_INITDB_OPTIONS=--auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8 "
            "postgresql-setup --initdb") in made


@systemd_here
def test_debian_makes_a_cluster_only_when_the_package_did_not(tmp_path):
    calls = tmp_path / "calls"
    answer = bash(tmp_path, postgresql_fixture("apt") + "\ninstall_postgresql_packages",
                  env={"CALLS": str(calls), "PATH": fake_programs(tmp_path, "pg_lsclusters")})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    made = calls.read_text()
    assert "sudo apt-get update" in made and "postgresql postgresql-client" in made
    assert "pg_createcluster" in made and "initdb" not in made


def test_a_failed_package_install_says_how_to_put_it_right(tmp_path):
    answer = bash(tmp_path, postgresql_fixture("pacman") + "\ninstall_postgresql_packages",
                  env={"CALLS": str(tmp_path / "calls"), "PACKAGE_STATUS": "1"})
    assert answer.returncode == 78
    said = answer.stderr
    assert "PostgreSQL could not be installed with pacman." in said
    assert "sudo pacman -Syu" in said
    assert "Check it installs by hand: sudo pacman -S --needed postgresql" in said
    assert "install.sh --database sqlite" in said


@pytest.mark.parametrize("listing,needed", [
    ("8442 1025-65535/tcp 1025-65535/udp", False), ("8442 8440/tcp", True), ("8442 8442/tcp", False),
    ("8442 8000:9000/tcp", False), ("8442 8442/udp", True), ("443 80,443/tcp", False), ("8442 1-65535", False),
])
def test_open_ports_and_ranges_are_read_as_firewalls_print_them(tmp_path, listing, needed):
    port, _, rest = listing.partition(" ")
    answer = bash(tmp_path, f'port_listed {port} "{rest}"')
    assert (answer.returncode != 0) == needed


def test_ufw_rules_file_gives_its_open_tcp_ports(tmp_path):
    rules = tmp_path / "user.rules"
    rules.write_text("*filter\n"
                     "-A ufw-user-input -p udp --dport 53317 -j ACCEPT\n"
                     "-A ufw-user-input -p tcp --dport 22 -j ufw-user-limit-accept\n"
                     "-A ufw-user-input -p tcp -m multiport --dports 8440,8442 -j ACCEPT\n"
                     "-A ufw-user-input -s 192.168.1.0/24 -j ACCEPT\n"
                     "-A ufw-user-input -p udp --dport 4242 -j ACCEPT\n")
    answer = bash(tmp_path, f'ufw_rules_ports "{rules}"')
    assert answer.stdout.split() == ["22", "8440,8442", "1-65535"]


def test_firewalld_that_keeps_the_port_closed_gets_the_command_that_opens_it(tmp_path):
    firewall = "#!/bin/sh\ncase \"$1\" in --state) echo running ;; --list-ports) echo \"$PORTS\" ;; esac\n"
    path = fake_programs(tmp_path, "firewall-cmd", script=firewall)
    source = 'firewall_steps 8440 8442\nprintf "%s\\n" "$FIREWALL_NAME" "$FIREWALL_CLOSED" "$FIREWALL_SURE" "${FIREWALL_STEPS[@]}"'
    closed = bash(tmp_path, source, env={"PATH": path, "PORTS": "22/tcp 8440/tcp"})
    assert closed.stdout.splitlines() == [
        "firewalld", "8442", "1",
        "Open it for good: sudo firewall-cmd --permanent --add-port=8442/tcp && sudo firewall-cmd --reload"]
    open_range = bash(tmp_path, 'firewall_steps 8440 8442\necho "${#FIREWALL_STEPS[@]}"',
                      env={"PATH": path, "PORTS": "1025-65535/tcp 1025-65535/udp"})
    assert open_range.stdout.strip() == "0"


def llama_fixture() -> str:
    """install-llamacpp.sh's own library checks, with a stand-in ldd."""
    return ('SELF=install-llamacpp.sh BACKEND=cpu ASSUME_YES=1\nLIBRARY_PROBLEM="" LIBRARY_STEPS=()\n'
            + "\n".join(one_function("scripts/install-llamacpp.sh", name) for name in
                        ("missing_libraries", "missing_versions", "source_steps", "supply_libraries")))


def test_a_prebuilt_llama_cpp_too_new_for_the_c_library_points_at_a_build_here(tmp_path):
    answer = bash(tmp_path, llama_fixture() + r'''
ldd() {
  if [[ $1 == --version ]]; then echo "ldd (Debian GLIBC 2.36-9+deb12u7) 2.36"; return 0; fi
  echo "$1: /lib/x86_64-linux-gnu/libc.so.6: version \`GLIBC_2.38' not found (required by $1)"
  echo "        libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)"
}
supply_libraries "$PWD" && exit 9
printf '%s\n' "$LIBRARY_PROBLEM" "${LIBRARY_STEPS[@]}"''', env={"PATH": fake_programs(tmp_path, "apt-get")})
    assert answer.returncode == 0, answer.stderr
    said = answer.stdout.splitlines()
    assert said[0] == "This prebuilt llama.cpp needs GLIBC_2.38, newer than this system's C library (2.36)."
    assert said[1] == "Install the build tools: sudo apt-get install git cmake build-essential"
    assert said[2] == "Build llama.cpp on this machine: install-llamacpp.sh --from-source --backend cpu"


def test_a_cuda_build_without_the_nvidia_driver_offers_vulkan_and_cpu(tmp_path):
    answer = bash(tmp_path, llama_fixture() + r'''
ldd() { [[ $1 == --version ]] && return 0; echo "        libcuda.so.1 => not found"; }
supply_libraries "$PWD" && exit 9
printf '%s\n' "$LIBRARY_PROBLEM" "${LIBRARY_STEPS[@]}"''')
    assert answer.returncode == 0, answer.stderr
    assert "The NVIDIA driver is not installed" in answer.stdout
    assert "install-llamacpp.sh --backend vulkan" in answer.stdout and "install-llamacpp.sh --backend cpu" in answer.stdout


@pytest.mark.parametrize("version,suffix", [("0.105", ".pkla"), ("124", ".rules")])
def test_the_power_rule_matches_the_polkit_it_is_for(tmp_path, version, suffix):
    written = tmp_path / "written"
    answer = bash(tmp_path, f'pkaction() {{ echo "pkaction version {version}"; }}\n' + r'''
DIR=$TEST_REPO OWG_OWNER=fixture OWG_RETRY_COMMAND=install.sh
ui_sudo() { :; }
sudo() {
  [[ $1 == -n ]] && shift
  case "$1" in tee) cat >"$WRITTEN" ;; esac
  return 0
}
''' + one_function("install.sh", "power_rule") + '\npower_rule\necho "$POLKIT_RULE"',
                  env={"WRITTEN": str(written)})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip().splitlines()[-1].endswith(suffix)
    rule = written.read_text()
    if suffix == ".pkla":
        assert "Identity=unix-user:fixture" in rule
        assert "org.freedesktop.login1.power-off*" in rule and "ResultActive=yes" in rule
    else:
        assert 'var ACCOUNT = "fixture";' in rule and "var SERVICES = [];" in rule


@pytest.mark.parametrize("script", ["install.sh", "scripts/install-suite.sh"])
def test_an_unexpected_failure_keeps_its_exit_status_and_says_where(tmp_path, script):
    trap = re.search(r"(?m)^ *(trap 'on_error .*' ERR)$", (ROOT / script).read_text())[1]
    answer = bash(tmp_path, "set -Eeuo pipefail\nOWG_RETRY=''\n" + one_function(script, "on_error") + "\n" + trap
                  + "\nfailing() { return 3; }\nfailing\necho not reached")
    assert "not reached" not in answer.stdout
    assert answer.returncode == (3 if script == "install.sh" else 78), answer.stderr
    assert "stopped unexpectedly" in answer.stderr and "(exit status 3)" in answer.stderr
    assert "return 3" in answer.stderr


def test_awk_is_installed_before_anything_needs_it(tmp_path):
    calls = tmp_path / "calls"
    answer = bash(tmp_path, r'''
PKG_MANAGER=zypper PKG_INSTALL=(zypper --non-interactive install)
ui_sudo() { :; }
command() { [[ $1 == -v && $2 == awk ]] && return 1; builtin command "$@"; }
sudo() { [[ $1 == -n ]] && shift; printf '%s\n' "$*" >>"$CALLS"; }
''' + one_function("install.sh", "install_packages") + "\n" + one_function("install.sh", "core_tools") + "\ncore_tools",
                  env={"CALLS": str(calls)})
    assert answer.returncode == 0, answer.stderr
    assert calls.read_text() == "zypper --non-interactive install gawk\n"


def test_time_zones_work_without_the_system_database():
    """Minimal Debian and Ubuntu systems have no /usr/share/zoneinfo, and weightroom's default zone
    must still load there: the tzdata package supplies it."""
    import sys
    answer = subprocess.run([sys.executable, "-c", "import zoneinfo; zoneinfo.reset_tzpath([]); "
                             "zoneinfo.ZoneInfo('America/Vancouver')"], capture_output=True, text=True)
    assert answer.returncode == 0, answer.stderr


@pytest.mark.parametrize("system,dir_given,expected", [
    (1, 0, "curl -fsSL https://raw.example/main/install.sh | bash -s -- --system"),
    (0, 1, "curl -fsSL https://raw.example/main/install.sh | bash -s -- --dir /srv/gym"),
    (0, 0, "curl -fsSL https://raw.example/main/install.sh | bash"),
])
def test_the_retry_command_is_built_without_tripping_the_error_trap(tmp_path, system, dir_given, expected):
    answer = bash(tmp_path, "set -Eeuo pipefail\ntrap 'echo TRAPPED; exit 99' ERR\n"
                  f"DIR=/srv/gym SYSTEM={system} DIR_GIVEN={dir_given} ACTION='' "
                  "OWG_RAW=https://raw.example OWG_REF=main\n"
                  + one_function("install.sh", "set_retry") + '\nset_retry\necho "$OWG_RETRY_COMMAND"')
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert answer.stdout.strip() == expected


def test_an_unexpected_failure_in_the_piped_installer_names_install_sh(tmp_path):
    answer = bash(tmp_path, "set -Eeuo pipefail\nOWG_RETRY=''\n" + one_function("install.sh", "on_error")
                  + "\non_error 2 main 148 'false'")
    assert answer.returncode == 2
    assert "at install.sh line 148" in answer.stderr


# ---- why a step failed, in words, with the fix for this system --------------------------------

@pytest.mark.parametrize("manager,os_id,log,why,step", [
    ("apt", "ubuntu", "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device",
     "The disk is full", "df -h"),
    ("dnf", "fedora", "bash: line 1: 4242 Killed                  cmake --build build", "ran out of memory", "dmesg"),
    ("apt", "debian", "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 812 (unattended-upgr)",
     "Another program is installing", "sudo dpkg --configure -a"),
    ("pacman", "arch", "error: failed to init transaction (unable to lock database)", "Another program is installing",
     "sudo rm /var/lib/pacman/db.lck"),
    ("pacman", "arch", 'error: python: signature from "Some Packager <p@example.org>" is unknown trust',
     "signing keys", "sudo pacman -Sy archlinux-keyring"),
    ("apt", "ubuntu", "E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/main/p/postgresql-16/x.deb  404  Not Found [IP: 1.2.3.4 80]",
     "package lists here are out of date", "sudo apt-get update"),
    ("apt", "ubuntu", "  ╰─▶ dns error: failed to lookup address information: Temporary failure in name resolution\n"
     "      error sending request for url (https://files.pythonhosted.org/packages/numpy.whl)",
     "could not reach files.pythonhosted.org", "curl -sSI https://files.pythonhosted.org"),
    ("zypper", "opensuse-tumbleweed", "curl: (60) SSL certificate problem: unable to get local issuer certificate",
     "certificate could not be checked", "sudo zypper install ca-certificates"),
    ("dnf", "rocky", "No match for argument: pgvector", "not found in this system's software sources", "sudo crb enable"),
    ("apt", "ubuntu", "error: command 'gcc' failed: No such file or directory", "C and C++ compiler",
     "sudo apt-get install git cmake build-essential"),
    ("zypper", "opensuse-leap", "bash: line 1: cmake: command not found", "cmake is not installed", "sudo zypper install cmake"),
    ("apt", "ubuntu", "[Errno 98] error while attempting to bind on address ('0.0.0.0', 8440): address already in use",
     "(8440) is taken", 'sport = :8440'),
    ("apt", "debian", "ERROR:    [Errno 98] error while attempting to bind on address ('0.0.0.0', 8440): [errno 98] address already in use",
     "(8440) is taken", 'sport = :8440'),
    ("zypper", "opensuse-microos", "mkdir: cannot create directory '/usr/lib/x': Read-only file system", "read-only",
     "transactional-update"),
    ("dnf", "fedora", "sudo: a password is required", "could not ask for your password", "sudo -v"),
])
def test_a_failed_steps_own_words_name_the_cause_and_the_fix_for_this_system(tmp_path, manager, os_id, log, why, step):
    answer = bash(tmp_path, f'PKG_MANAGER={manager} OS_ID={os_id} OS_NAME={os_id} PKG_INSTALL=({manager})\n'
                            'diagnose_log "$LOG_TEXT"\nprintf "WHY %s\\n" "${DIAGNOSIS[@]}"\nprintf "STEP %s\\n" "${DIAGNOSIS_STEPS[@]}"',
                  env={"LOG_TEXT": log})
    assert answer.returncode == 0, answer.stderr
    whys = [line for line in answer.stdout.splitlines() if line.startswith("WHY ")]
    assert any(why in line for line in whys), answer.stdout
    assert step in answer.stdout


def test_a_permission_problem_in_the_home_folder_says_whose_file_and_how_to_take_it_back(tmp_path):
    answer = bash(tmp_path, 'PKG_MANAGER=apt PKG_INSTALL=(apt)\n'
                            'diagnose_log "PermissionError: [Errno 13] Permission denied: \'$HOME/openWeightGym/.venv/lib\'"\n'
                            'printf "%s\\n" "${DIAGNOSIS[@]}" "${DIAGNOSIS_STEPS[@]}"')
    assert answer.returncode == 0, answer.stderr
    assert "(~/openWeightGym/.venv/lib)" in answer.stdout
    assert "ls -ld ~/openWeightGym/.venv/lib" in answer.stdout
    assert f"sudo chown -R {os.environ.get('USER') or subprocess.check_output(['id', '-un'], text=True).strip()}: ~/openWeightGym/.venv/lib" in answer.stdout


def test_ordinary_output_names_no_cause(tmp_path):
    answer = bash(tmp_path, 'PKG_MANAGER=apt PKG_INSTALL=(apt)\ndiagnose_log "Collecting numpy\nInstalling collected packages: numpy\n'
                            'error: subprocess-exited-with-error"\necho "${#DIAGNOSIS[@]} ${#DIAGNOSIS_STEPS[@]}"')
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip() == "0 0"


def test_the_stop_panel_says_why_and_leads_with_the_fix_under_the_installers_error_trap(tmp_path):
    answer = bash(tmp_path, '''set -Eeuo pipefail
trap 'echo TRAPPED >&2' ERR
PKG_MANAGER=apt PKG_INSTALL=(apt)
OWG_RETRY="Run the installer again: ~/openWeightGym/install.sh" OWG_STOP_TITLE="The install stopped"
full() { echo "Collecting numpy"; echo "OSError: [Errno 28] No space left on device"; return 1; }
ui_step "Installing Python packages" -- full ||
  ui_die "openWeightGym's Python packages did not install." "The lines above, from the log, say why: put that right" \\
    "If the log shows a package being built, install the build tools"''')
    assert answer.returncode == 78
    err = answer.stderr
    assert "TRAPPED" not in err
    assert re.search(r"FAILED .* The install stopped", err)
    assert "Why: The disk is full" in err
    steps = re.findall(r"(?m)^\s*[|┃] +(\d)\. (.*)$", err)
    assert [text for _, text in steps][:2] == [
        "See how much room each drive has: df -h",
        "Make room on the full one: the download caches ~/.cache/uv and ~/.cache/pip are safe to delete, and models are the largest files"]
    assert "The lines above, from the log" not in err
    assert "If the log shows a package being built" in err
    assert steps[-1][1] == "Run the installer again: ~/openWeightGym/install.sh"


def test_a_stop_after_a_step_that_worked_blames_nothing_that_step_wrote(tmp_path):
    answer = bash(tmp_path, '''PKG_MANAGER=apt PKG_INSTALL=(apt)
noisy() { echo "warning: No space left on device in a cache it cleared itself"; }
ui_step "Fetching" -- noisy
ui_die "The settings could not be read." "Check them"''')
    assert answer.returncode == 78
    assert "Why:" not in answer.stderr
    assert "1. Check them" in answer.stderr


def stops_without_steps(path: str) -> list[str]:
    """Every die or ui_die in a script that gives no step to put things right."""
    lines = (ROOT / path).read_text().split("\n")
    found, i = [], 0
    while i < len(lines):
        line = lines[i]
        call = re.search(r"(?<![\w-])(ui_die|die|usage_error)\s", line)
        if not call or line.lstrip().startswith("#") or re.match(r"\s*(ui_die|die|usage_error)\(\)", line):
            i += 1
            continue
        text, j = line[call.start():], i
        while True:
            try:
                lexer = shlex.shlex(text.replace("\\\n", " "), posix=True, punctuation_chars=";&|")
                lexer.whitespace_split = True
                words = list(lexer)
                break
            except ValueError:  # a quoted message carries on onto the next line
                j += 1
                text += "\n" + lines[j]
        arguments = []
        for word in words[1:]:
            if word in (";", "||", "&&", "|", "}", ";;", ")", "&"):
                break
            arguments.append(word)
        if arguments[:1] == ["--status"]:
            arguments = arguments[2:]
        if words[0] != "usage_error" and len(arguments) <= 1:
            found.append(f"{path}:{i + 1}: {arguments[0].splitlines()[0] if arguments else '(no message)'}")
        i = j + 1
    return found


@pytest.mark.parametrize("path", ["install.sh", "uninstall.sh", "scripts/install-suite.sh", "scripts/install-llamacpp.sh",
                                  "scripts/owg", "scripts/lib/receipt.sh"])
def test_every_stop_says_what_to_do_next(path):
    assert stops_without_steps(path) == []


@pytest.mark.parametrize("command,status,message,step", [
    (["install.sh", "--dir"], 2, "--dir needs a folder.", "run the same command with --help"),
    (["install.sh", "--bogus"], 2, "The installer has no option --bogus.", "run the same command with --help"),
    (["install.sh", "--system", "--database", "postgresql"], 2, "keeps its data in SQLite", "docs/database-storage.md"),
    (["uninstall.sh", "--bogus"], 2, "The uninstaller has no option --bogus.", "run the same command with --help"),
    (["scripts/install-suite.sh", "--services"], 78, "--services needs a value", "install-suite.sh --help"),
    (["scripts/install-llamacpp.sh", "--backend", "nope"], 2, "not nope.", "install-llamacpp.sh --help"),
])
def test_a_mistyped_option_stops_loudly_before_anything_is_touched(tmp_path, command, status, message, step):
    home = tmp_path / "home"
    home.mkdir()
    answer = subprocess.run([str(ROOT / command[0]), *command[1:]], cwd=tmp_path, text=True, capture_output=True, timeout=30,
                            env={**os.environ, "OWG_NO_TTY": "1", "OWG_PLAIN": "1", "HOME": str(home),
                                 "XDG_STATE_HOME": str(home / ".local/state"), "OWG_LOG": str(tmp_path / "install.log")})
    assert answer.returncode == status, answer.stdout + answer.stderr
    assert "FAILED" in answer.stderr and "Nothing was changed" in answer.stderr or command[0].endswith("install-suite.sh")
    assert message in answer.stderr
    assert step in answer.stderr
    assert "unbound variable" not in answer.stderr
    assert list(home.iterdir()) in ([], [home / ".local"])


def test_the_piped_installer_that_cannot_fetch_its_parts_says_what_to_do(tmp_path):
    answer = subprocess.run(["bash"], input=(ROOT / "install.sh").read_text(), cwd=tmp_path, text=True, capture_output=True,
                            timeout=60, env={**os.environ, "OWG_RAW": "http://127.0.0.1:9/owg", "HOME": str(tmp_path)})
    assert answer.returncode == 1
    assert "FAILED: The installer could not download its parts from http://127.0.0.1:9/owg (branch main)." in answer.stderr
    assert "1. Check this machine can reach GitHub: curl -sSI https://raw.githubusercontent.com | head -1" in answer.stderr
    assert "4. Then run the installer again" in answer.stderr


def test_guesses_at_the_cause_give_way_to_the_cause_the_log_names(tmp_path):
    answer = bash(tmp_path, '''PKG_MANAGER=apt PKG_INSTALL=(apt)
fails() { echo "$1"; return 1; }
ui_step "Fetching" -- fails "curl: (6) Could not resolve host: pypi.org" || ui_failure "Fetching failed." "?Check the network" "Keep this step"
echo "-- second" >&2
ui_step "Fetching" -- fails "something odd" || ui_failure "Fetching failed." "?Check the network" "Keep this step"''')
    named, unknown = answer.stderr.split("-- second")
    assert "Why: This machine could not reach pypi.org" in named
    assert "Check the network" not in named and "Keep this step" in named
    assert "Why:" not in unknown
    assert re.search(r"1\. Check the network\n.*2\. Keep this step", unknown)
    assert "?" not in re.sub(r"(?s).*What to do next:", "", unknown)


def test_an_attention_item_can_say_why_on_a_line_of_its_own(tmp_path):
    answer = bash(tmp_path, 'ui_attention "weightroom is not running.\nWhy: A port openWeightGym needs (8440) is taken." '
                            '"See all it said: owg logs weightroom"\nui_attention_box')
    assert answer.returncode == 0, answer.stderr
    warning, box = answer.stdout.split("Needs your attention")
    assert "weightroom is not running." in warning and "Why:" not in warning
    assert re.search(r"(?m)^\s*[|┃]\s+weightroom is not running\.$", box)
    assert re.search(r"(?m)^\s*[|┃]\s+Why: A port openWeightGym needs \(8440\) is taken\.$", box)
    assert "1. See all it said: owg logs weightroom" in box
