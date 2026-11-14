"""Typed setting contracts shared by loaders, management APIs and presentation.

Tables are settings in their own right; their members are validated by the table's
contract. File-only entries describe machine configuration without making it editable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NOW, RESTART, FILE = 'now', 'restart', 'file'
SECTION_ORDER = (
    'Platform services', 'Requests and queue', 'Logs and retention', 'Power and idle',
    'Telemetry, power and idle', 'Alerts and monitoring', 'Tools', 'Display and costs',
    'Model files', 'Direct conversations', 'Hermes agent', 'Sessions and limits',
    'Speech', 'RAM policy', 'Additional settings', 'Advanced',
)


@dataclass(frozen=True)
class Setting:
    """One canonical setting, including its site editor and validation contract."""

    program: str
    name: str
    kind: str
    tier: str = FILE
    scope: str = 'node'
    page: str = 'machine'
    section: str = 'Additional settings'
    label: str = ''
    unit: str = ''
    min: float | None = None
    max: float | None = None
    choices: tuple = ()
    choices_from: str = ''
    help: str = ''
    retired: bool = False
    retired_when: str = ''
    derived: bool = False
    deprecated: bool = False
    control: str = ''
    rule: Callable | None = None
    exclusive_min: bool = False
    exclusive_max: bool = False
    ceiling: float | None = None

    def check(self, value, *, context=None):
        """Return a valid value, or raise ValueError without echoing its contents."""
        types = {'bool': (bool,), 'int': (int,), 'float': (int, float),
                 'text': (str,), 'choice': (str,), 'secret': (str,),
                 'list': (list, tuple), 'table': (dict,)}
        if type(value) not in types[self.kind]:
            words = {'bool': 'true or false', 'int': 'a whole number', 'float': 'a number',
                     'text': 'text', 'choice': 'text', 'secret': 'text', 'list': 'a list', 'table': 'a table'}
            raise ValueError(f'{self.name} must be {words[self.kind]}.')
        if self.kind in ('int', 'float'):
            if type(value) is float and not math.isfinite(value):
                raise ValueError(f'{self.name} must be finite.')
            if self.min is not None and (value < self.min or self.exclusive_min and value == self.min):
                raise ValueError(f'{self.name} must be {"above" if self.exclusive_min else "at least"} {self.min}.')
            if self.max is not None and (value > self.max or self.exclusive_max and value == self.max):
                raise ValueError(f'{self.name} must be {"below" if self.exclusive_max else "at most"} {self.max}.')
        if self.choices and value not in self.choices:
            raise ValueError(f'{self.name}: choose one of {", ".join(map(str, self.choices))}.')
        if self.choices_from and context and self.choices_from in context:
            if value and value not in context[self.choices_from]():
                raise ValueError(f'{self.name}: choose an available value, or Automatic.')
        if self.rule:
            self.rule(value)
        return value


def _timezone(value):
    try:
        if value:
            ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('timezone must name an available time zone, or be empty.') from None


def _absolute(value):
    if value and not value.startswith('/'):
        raise ValueError('Model folders must be a full path, or empty.')


def _distinct(value):
    if any(not isinstance(one, str) or not one for one in value) or len(value) != len(set(value)):
        raise ValueError('Use a list of distinct names.')


def _services(value):
    _distinct(value)
    if set(value) - {'ollama', 'docker', 'apache', 'nginx'}:
        raise ValueError('third_party_services may contain ollama, docker, apache and nginx once each.')


def _engine_gpus(value):
    from engines import engine_for
    for name, cards in value.items():
        engine_for(name)
        if not isinstance(cards, list):
            raise ValueError('engine_gpus is a table of engine IDs and card lists.')
        _distinct(cards)


def _engines(value):
    from engines import checked_installations
    checked_installations(value, value.get('llama_cpp', {}).get('executable', 'llama-server'))


def _component_interval(value):
    from loadcoach.component_versions import interval_seconds
    interval_seconds(value)


def _greetings(value):
    from voice.prompt_recordings import texts
    texts({'greetings': value})


def _telephone(value):
    from voice.telephone_settings import validate
    validate(value)


def _console_owner(value):
    if not value:
        raise ValueError('keep_console_settings_on_front=false is no longer supported; the control node stores its console settings.')


def _toolsets(value):
    for name, toolset in value.items():
        tools = toolset.get('tools') if isinstance(toolset, dict) and toolset.keys() == {'tools'} else None
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError('toolsets needs lowercase collection names and tools, a list of names.')


def _tool_levels(value):
    from loadcoach.groups import LEVELS
    if not all(isinstance(tool, str) and level in LEVELS for tool, level in value.items()):
        raise ValueError(f'tool_levels gives each tool one of {", ".join(LEVELS)}.')


def validate_patch(program, current, patch, *, context=None):
    """Validate a whole POST before writing, including table relationships and resets."""
    effective = {**current, **{name: value for name, value in patch.items() if value is not None}}
    for name, value in patch.items():
        if value is not None:
            validate(program, name, value, context=context)
    if program == 'turnstile' and {'models_folder', 'models_storage_folder'} & patch.keys():
        from turnstile.artifacts import checked_roots
        checked_roots({name: effective.get(name, '') for name in ('models_folder', 'models_storage_folder')})
    return effective


def _policy(name):
    def check(value):
        from loadcoach.observation_store import POLICY, check_policy
        values = {**POLICY, name.removeprefix('observation_'): value}
        check_policy(**values)
    return check


def _alert(name):
    def check(value):
        from loadcoach.observability import alert_value
        alert_value(name, value)
    return check


def _disks(value):
    if len(value) > 32 or any(not isinstance(path, str) or not path.startswith('/') or '\0' in path for path in value):
        raise ValueError('disks must contain at most 32 absolute paths.')


# Each service's entries are explicit: adding a setting here is the only way to
# grant site editability. No file supplied by an operator changes this catalogue.
CATALOG: dict[tuple[str, str], Setting] = {}


def _add(program, name, kind, **metadata):
    entry = Setting(program, name, kind, **metadata)
    if (program, name) in CATALOG:
        raise ValueError(f'Duplicate setting: {program}.{name}')
    CATALOG[program, name] = entry


def get(program, name):
    """Find a contract, including machine-owned per-node override namespaces."""
    return CATALOG.get(('machines' if program.startswith('machines:') else program, name))


def entries(program=None):
    return tuple(value for value in CATALOG.values() if program is None or value.program == program)


def validate(program, name, value, *, context=None):
    """The common loader/site validation entry point; unknown keys remain file-only."""
    entry = get(program, name)
    if entry is None:
        raise ValueError(f'Unknown setting: {program}.{name}')
    return entry.check(value, context=context)


def validate_values(program, values, *, context=None):
    """Validate known configured values without rejecting a service's runtime metadata."""
    for name, value in values.items():
        if get(program, name):
            validate(program, name, value, context=context)
    return values


def changeable():
    result = {}
    for setting in CATALOG.values():
        if setting.tier != FILE and not (setting.retired or setting.derived):
            result.setdefault(setting.program, {})[setting.name] = setting.tier
    return result


def retired_for(program, name, context=None):
    """A conditional retirement must not retire the same key on standalone nodes."""
    entry = get(program, name)
    return bool(entry and (entry.retired or entry.retired_when and (context or {}).get(entry.retired_when)))


def changeable_for(program, context=None):
    return {entry.name: entry.tier for entry in entries(program)
            if entry.tier != FILE and not entry.derived and not retired_for(program, entry.name, context)}


# Existing service settings.

# loadcoach
_add('loadcoach', 'host', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Host', help='Address and port LoadCoach listens on. Loopback means this machine only.')
_add('loadcoach', 'port', 'int', tier='file', scope='node', page='machine', section='Additional settings', label='Port', min=0)
_add('loadcoach', 'data', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Data', help='The folder LoadCoach keeps its database and the token for the `loadcoach` command in, counted from this file. Left out, they are kept beside this file.')
_add('loadcoach', 'machine_key_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine key file', help="The key this machine shares with the control node, counted from this file. Turnstile signs what it sends on with it, and LoadCoach, the ToolLocker, spotter and voice believe nothing else about who is asking. Readable by you alone, and the same on both machines: python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ../keys/machine.key && chmod 600 ../keys/machine.key Left out, nobody but this machine itself may ask, as anyone: only for a host of 127.0.0.1.")
_add('loadcoach', 'machine_name', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine name', help='This instance\'s stable name in the control node\'s [machines.NAME] registry. The primary is "compute"; a second instance might be "rig". Give every instance its own key, data folder and port.')
_add('loadcoach', 'front', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Front', help="Turnstile's address on the control node. Compute asks it for the accounts, for work done later in someone's name: a batch, a benchmark task, a recurring schedule, and for the reports.")
_add('loadcoach', 'llama_server', 'text', tier='restart', scope='node', page='machine', section='Additional settings', label='Llama server', help="Compatibility path for llama.cpp. A service does not always have your shell's PATH. Prefer [engines.llama_cpp] below for new configuration; if both paths exist they must match.")
_add('loadcoach', 'models_folder', 'text', tier='restart', scope='node', page='general', section='Model files', label='Active model cache', control='text', help='Optional common model root. When set, LoadCoach uses its `llm`, `adapters`, `projectors`, `image` `audio` and `encoders` subfolders instead of [places]. It can be changed in Settings and takes effect after restart.', rule=_absolute)
_add('loadcoach', 'models_storage_folder', 'text', tier='restart', scope='node', page='general', section='Model files', label='Model storage library', control='text', help="Optional complete model library, separate from the active cache above. Downloads prefer its `llm`, `adapters`, `projectors`, `image`, `audio` and `encoders` folders. Empty keeps a new installation's storage disabled.", rule=_absolute)
_add('loadcoach', 'load_timeout', 'float', tier='now', scope='node', page='requests', section='Requests and queue', label='Model load timeout', unit='s', control='number', min=0, help='Seconds to wait for a model to load before giving up.')
_add('loadcoach', 'max_wait', 'float', tier='now', scope='node', page='requests', section='Requests and queue', label='Queue wait limit', unit='s', control='number', min=0, help='Seconds a request may wait in line, loading included, before it is told to try again.')
_add('loadcoach', 'max_estimated_wait', 'float', tier='now', scope='node', page='requests', section='Requests and queue', label='Estimated wait limit', unit='s', control='number', min=0, max=1000000000000.0, exclusive_max=True, help='Optional estimated-wait admission limit in seconds. 0 disables it. When enabled, unknown or longer estimates are refused immediately; max_wait remains the actual queue timeout. Rates come from measured use; prompt sizes and maximum output budgets are inferred work.')
_add('loadcoach', 'card_margin', 'int', tier='now', scope='node', page='nodes', section='RAM policy', label='GPU memory headroom', unit='MiB', control='number', min=0, help='MiB of each card that are kept free, on top of what the models on it were measured to use. A model grows a little while it answers, and this is the room for that. Smaller lets more models share a card, and makes it likelier that a load runs out of memory and is tried again.')
_add('loadcoach', 'engine_gpus', 'table', tier='now', scope='node', page='machine', section='Additional settings', label='Engine gpus', help='Per-engine GPU assignments, edited under System → Compute nodes. An omitted engine may use every compatible enabled card; an empty list permits no GPU work for that engine. For example: { llama_cpp = ["CUDA0"], sdcpp = ["CUDA1"], audiocpp = ["CUDA1"] }', rule=_engine_gpus)
_add('loadcoach', 'model_idle_minutes', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='GPU model idle timeout', unit='min', control='number', min=0, help='Optional minutes an idle model is protected from replacement after its last request. 0 keeps models resident while room is available, but lets a new request replace them immediately when it needs their memory. This is not an automatic unload timer. Active requests, holds and model locks always stay protected. Positive values add a wait.')
_add('loadcoach', 'host_ram_headroom_mib', 'int', tier='now', scope='node', page='nodes', section='RAM policy', label='Host RAM headroom', unit='MiB', control='number', min=0, help='MiB of host RAM reserved from CPU model admission, within existing service memory limits.')
_add('loadcoach', 'host_ram_limit_mib', 'int', tier='now', scope='node', page='nodes', section='RAM policy', label='Host RAM limit', unit='MiB', control='number', min=0, help="Optional MiB budget for CPU model processes on this node. 0 uses the smaller of host availability and the service's systemd/cgroup memory ceiling.")
_add('loadcoach', 'disable_ram_offload', 'bool', tier='restart', scope='node', page='nodes', section='RAM policy', label='Disable RAM offload', control='switch', help='Keep GPU models in VRAM and fail admission/loading instead of allowing CUDA unified-memory spill into host RAM. Changing this requires a LoadCoach restart.')
_add('loadcoach', 'cpu_idle_minutes', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='CPU model idle timeout', unit='min', control='number', min=0, help='Unload CPU models after this many idle minutes; 0 disables automatic CPU eviction. Active requests, holds, locks and GPU models stay protected.')
_add('loadcoach', 'keep_days', 'float', tier='now', scope='node', page='retention', section='Logs and retention', label='Event retention', unit='days', control='number', min=0, help='Standalone event retention in days; 0 keeps everything. Retired on nodes with a control node; acknowledged local copies retain their fixed bound.', retired_when='front')
_add('loadcoach', 'batch_keep_days', 'float', tier='now', scope='node', page='retention', section='Logs and retention', label='Batch retention', unit='days', control='number', min=0, help='Days after a batch has ended that its files are kept: the requests that were handed in, and the answers. After that both are removed, and the batch stays listed with its counts. 0 keeps them for ever.')
_add('loadcoach', 'keep_text', 'int', tier='now', scope='node', page='retention', section='Logs and retention', label='Request text retention', control='number', min=0, help='How many of the latest requests are kept in full: what was sent and what came back, whatever their level. It is for finding out what went wrong. The owner of a request can read its text, and so can those who may "see-capture". 0 keeps none.')
_add('loadcoach', 'unlogged', 'bool', tier='now', scope='node', page='retention', section='Logs and retention', label='Allow requests without logs', control='switch', help='Whether a request may be sent that leaves nothing behind: no entry in the event log, no request number, no kept text. false, as here, refuses every such request. With true, an account that has the ability "unlogged" may send one, by adding this header to it: X-LoadCoach-Log: off Such a request is not counted in anyone\'s usage, cannot be marked sensitive or given a piece of work, and cannot go to a hosted model. Asking for one without the ability is refused, and that refusal is put on record.')
_add('loadcoach', 'model_defaults', 'table', tier='now', scope='node', page='nodes', section='Advanced', label='Inherited loading flags', control='table', help='Flags that every model gets, unless its own section sets a different value for the same flag. These three shrink the memory a model\'s context takes to about half, measured here at 53 %, by keeping it at 8 bits a number, not 16. The effect on answers is usually too small to notice. A model that should keep full precision sets cache-type-k and cache-type-v to "f16". "q4_0" halves it again, to 28 %, at a cost that can be noticed.')
_add('loadcoach', 'engines', 'table', tier='restart', scope='node', page='machine', section='Additional settings', label='Engines', help='Per-node inference-engine overrides, edited under System > Compute nodes. Empty preserves llama.cpp enabled with the legacy llama_server path. Executable/enablement changes require a node restart; saving never installs programs or interrupts loaded models.', rule=_engines)
_add('loadcoach', 'models', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Models')
_add('loadcoach', 'places', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Places')
_add('loadcoach', 'hub_token_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Hub token file')
_add('loadcoach', 'civitai_token_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Civitai token file')
_add('loadcoach', 'gpus', 'list', tier='file', scope='node', page='machine', section='Additional settings', label='Gpus')
_add('loadcoach', 'disabled_gpus', 'list', tier='now', scope='node', page='machine', section='Additional settings', label='Disabled gpus', rule=_distinct)
_add('loadcoach', 'former_machine_name', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Former machine name')
_add('loadcoach', 'toollocker_runs_on_front', 'bool', tier='file', scope='node', page='machine', section='Additional settings', label='Toollocker runs on front')
_add('loadcoach', 'databases', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Databases')
_add('loadcoach', 'nvidia_smi', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Nvidia smi')

# spotter
_add('spotter', 'host', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Host', help="Address and port spotter listens on. It is asked by the control node, so give compute's own address.")
_add('spotter', 'port', 'int', tier='file', scope='node', page='machine', section='Additional settings', label='Port', min=0)
_add('spotter', 'data', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Data', help='The folder spotter keeps its readings in, counted from this file: spotter.toml gives spotter.db there. Left out, beside this file.')
_add('spotter', 'disks', 'list', tier='file', scope='node', page='machine', section='Additional settings', label='Disks', help='Folders whose drives are watched for room.', rule=_disks)
_add('spotter', 'services', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Services', help='The services spotter may start, stop and restart: a name of your choosing = the unit it is. Units of this account, the ones `systemctl --user` shows. Nothing that is not listed here or below can be touched, whatever is asked.')
_add('spotter', 'system_services', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='System services', help="The same for root's units. Listing one here is not enough: this account must also be allowed to act on it, by the polkit rule in services/spotter.rules, which names the same units and which root installs. Without the rule, the buttons are there and systemd refuses.")
_add('spotter', 'sample_every', 'float', tier='now', scope='node', page='alerts', section='Alerts and monitoring', label='Sampling interval', unit='s', control='number', min=1, help='Seconds between the readings that are kept, and the days they are kept for. A reading is about 400 bytes: every 30 seconds for 28 days is 37 MB.')
_add('spotter', 'keep_days', 'float', tier='now', scope='node', page='retention', section='Logs and retention', label='Event retention', unit='days', control='number', min=0)
_add('spotter', 'sleep_after', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='Sleep after idle', unit='min', control='number', min=0, help='Minutes with nothing using the machine before spotter puts it to sleep. 0, or left out, means never. "Using" is any of: a request or tool call at LoadCoach, a key or the mouse touched at the machine\'s own screen, a GPU at `busy_gpu` percent or more, the processor at `busy_processor` percent or more. Someone signed in over SSH and doing nothing does not count. A job that must not be put to sleep under can say so itself: systemd-inhibit --what=sleep --why="a long job" COMMAND Before turning this on, make sure the machine can be woken again: see wake_mac in settings/examples/weightroom.toml. A machine that sleeps and cannot be woken has to be walked to.')
_add('spotter', 'busy_gpu', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='GPU activity threshold', unit='%', control='number', min=0)
_add('spotter', 'busy_processor', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='CPU activity threshold', unit='%', control='number', min=0)
_add('spotter', 'files_every', 'float', tier='now', scope='node', page='retention', section='Logs and retention', label='Check model files every', unit='min', control='number', min=0, help="Minutes between looks through LoadCoach's model-file places, which keep the Files page's list of this node's files. Only files that are new or have changed since are read. The Files page answers from that list; its Refresh button looks again at once, and the files the page moves, copies, deletes or downloads are put right straight away. 0 means only when asked.")
_add('spotter', 'component_versions_every', 'float', tier='now', scope='node', page='alerts', section='Alerts and monitoring', label='Component version check interval', unit='min', control='number', min=0, help='Check installed service and engine versions this often, in minutes (above 0). Changes apply immediately.', rule=_component_interval)
_add('spotter', 'compute', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Compute')
_add('spotter', 'machine_key_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine key file')
_add('spotter', 'machine_name', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine name')
_add('spotter', 'databases', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Databases')
_add('spotter', 'turnstile_notice', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Turnstile notice')
_add('spotter', 'places', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Places')
_add('spotter', 'loadcoach', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Loadcoach')

# toollocker
_add('toollocker', 'host', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Host', help='Address and port the ToolLocker listens on. Loopback means this machine only.')
_add('toollocker', 'port', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Port', min=0)
_add('toollocker', 'front', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Front', help='WeightRoom owns the accounts/event store and Turnstile\'s machine key. No local LoadCoach is needed. Turnstile authenticates callers on public port 8443 and signs requests for this private gateway. Its [turnstile].toollocker_url must be "http://127.0.0.1:9443".')
_add('toollocker', 'servers', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Servers', help='Listing and reading files under the folders given as arguments. Nothing is written. Hidden files and files only their owner may read are never shown. Do not name a folder that holds secrets in ordinary files. To allow several folders, add each as one more entry at the end of the list. A list may run over several lines. Everything below a folder is included, so name a folder once, not its sub-folders as well.')
_add('toollocker', 'call_timeout', 'float', tier='now', scope='front', page='tools', section='Tools', label='Tool call timeout', unit='s', control='number', min=0, exclusive_min=True, help='Seconds one tool call may take. After that the call fails and its server is started afresh.')
_add('toollocker', 'tool_levels', 'table', tier='now', scope='front', page='tools', section='Tools', label='Tool access levels', control='table', help='A level for one tool, by the name a harness sees, in place of its server\'s level: "normal", "sensitive" or "restricted". It may be higher or lower than the server\'s. A caller sees a tool only if their group\'s max_sensitivity is at least its level, and from "sensitive" up each call is kept in full. Changed on System -> Tools, it holds within seconds.', rule=_tool_levels)
_add('toollocker', 'toolsets', 'table', tier='now', scope='front', page='tools', section='Tools', label='Tool collections', control='table', rule=_toolsets)
_add('toollocker', 'machine_key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Machine key file')
_add('toollocker', 'databases', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Databases')
_add('toollocker', 'extraction_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Extraction url')
_add('toollocker', 'loadcoach', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Loadcoach')

# voice-gateway
_add('voice-gateway', 'host', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Host', help='Control node. Bind the private page locally; Tailscale Serve supplies trusted HTTPS.')
_add('voice-gateway', 'port', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Port', min=0)
_add('voice-gateway', 'turnstile', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Turnstile')
_add('voice-gateway', 'voice_turnstile', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Voice turnstile')
_add('voice-gateway', 'voice', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Voice', help="Compute's voice service, for the sound socket alone. It is opened with the session's own token, never a caller's key; everything else goes through turnstile.")
_add('voice-gateway', 'key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Key file')
_add('voice-gateway', 'browser_key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Browser key file', help='The page asks for no name and no password. Everyone who opens it is this one account, as every telephone caller is the account in [twilio] key_file. What the account may do is what a caller may do: it needs `front-voice` and access to the voice and Hermes models. Turnstile checks it like any key, and wakes compute for it.')
_add('voice-gateway', 'data', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Data', help='WeightRoom on this machine. A call in a SoundRoom voice keeps its exchanges there as a conversation of the browser account (see soundroom_profile). conversations_url = "http://127.0.0.1:8440" The folder the copy of what was changed on the site is kept in, counted from this file.')
_add('voice-gateway', 'origins', 'list', tier='file', scope='front', page='machine', section='Additional settings', label='Origins')
_add('voice-gateway', 'modes', 'list', tier='now', scope='front', page='voice', section='Direct conversations', label='Conversation modes', control='text')
_add('voice-gateway', 'profile', 'text', tier='now', scope='front', page='voice', section='Direct conversations', label='Conversation profile', control='text')
_add('voice-gateway', 'soundroom_profile', 'text', tier='now', scope='front', page='voice', section='Speech', label='Audio model', control='text', help="The voice service profile for calls in a SoundRoom voice, whose speech is audio.cpp on the GPU (see settings/examples/voice.toml). Empty offers no voice choice: every call uses `profile`. When set, the browser page lists the browser account's SoundRoom voices.")
_add('voice-gateway', 'startup_seconds', 'float', tier='now', scope='front', page='voice', section='Sessions and limits', label='Startup timeout', unit='s', control='number', min=0)
_add('voice-gateway', 'max_active_sessions', 'int', tier='now', scope='front', page='voice', section='Sessions and limits', label='Active sessions', control='number', min=0)
_add('voice-gateway', 'public_port', 'int', tier='restart', scope='front', page='machine', section='Additional settings', label='Public port', min=0, help='Telephone-only public forwarding listener. Changes apply after gateway restart.')
_add('voice-gateway', 'public_host', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Public host')
_add('voice-gateway', 'hermes_model', 'text', tier='now', scope='front', page='voice', section='Hermes agent', label='Model', control='text', help="Hermes model. Empty uses the model in this gateway's [hermes] configuration.")
_add('voice-gateway', 'hermes_system_prompt', 'text', tier='now', scope='front', page='voice', section='Hermes agent', label='Instructions', control='text', help='Instructions for Hermes voice replies. Applies to new calls.')
_add('voice-gateway', 'hermes_max_turns', 'int', tier='now', scope='front', page='voice', section='Hermes agent', label='Agent turn limit', control='number', min=0, help='Maximum agent steps in a Hermes turn. Applies to new calls.')
_add('voice-gateway', 'hermes_context_tokens', 'int', tier='now', scope='front', page='voice', section='Hermes agent', label='Context limit', unit='tokens', control='number', min=0, help='Conversation context for the isolated Hermes agent. Applies to new calls.')
_add('voice-gateway', 'hermes_max_output_tokens', 'int', tier='now', scope='front', page='voice', section='Hermes agent', label='Output limit', unit='tokens', control='number', min=0, help="Maximum tokens per Hermes model response, including tool steps. Zero uses Hermes's default.")
_add('voice-gateway', 'hermes_reasoning_effort', 'text', tier='now', scope='front', page='voice', section='Hermes agent', label='Reasoning effort', control='text', help='Reasoning effort for Hermes calls. Applies to new calls.')
_add('voice-gateway', 'hermes', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Hermes')
_add('voice-gateway', 'twilio', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Twilio')
_add('voice-gateway', 'telephone', 'table', tier='now', scope='front', page='machine', section='Additional settings', label='Telephone', rule=_telephone)
_add('voice-gateway', 'conversations_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Conversations url')
_add('voice-gateway', 'desktop_host', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Desktop host')
_add('voice-gateway', 'desktop_port', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Desktop port', min=0)
_add('voice-gateway', 'context_key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Context key file')
_add('voice-gateway', 'max_starting_sessions', 'int', tier='now', scope='front', page='voice', section='Sessions and limits', label='Starting sessions', control='number', min=0)
_add('voice-gateway', 'observability_enabled', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Observability enabled')
_add('voice-gateway', 'observability_export_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Observability export url')

# voice
_add('voice', 'host', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Host', help='Compute. Speech weights are downloaded by scripts/voice-models.py.')
_add('voice', 'port', 'int', tier='file', scope='node', page='machine', section='Additional settings', label='Port', min=0)
_add('voice', 'compute', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Compute', help='Where the voice service asks LoadCoach for what it needs itself, with its own key: holds, the status and its settings. That is turnstile, on the control node, which checks the key.')
_add('voice', 'key_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Key file')
_add('voice', 'machine_key_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine key file', help='The key turnstile signs with: who a caller is comes in its statement, never as a key.')
_add('voice', 'data', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Data', help='The folder the copy of what was changed on the site is kept in, counted from this file.')
_add('voice', 'recognition', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Recognition')
_add('voice', 'synthesis', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Synthesis')
_add('voice', 'max_active_sessions', 'int', tier='now', scope='node', page='voice', section='Sessions and limits', label='Active sessions', control='number', min=0)
_add('voice', 'max_starting_sessions', 'int', tier='now', scope='node', page='voice', section='Sessions and limits', label='Starting sessions', control='number', min=0)
_add('voice', 'max_session_minutes', 'float', tier='now', scope='node', page='voice', section='Sessions and limits', label='Session duration limit', unit='min', control='number', min=0)
_add('voice', 'idle_seconds', 'float', tier='now', scope='node', page='voice', section='Sessions and limits', label='Idle session timeout', unit='s', control='number', min=0)
_add('voice', 'hold_seconds', 'float', tier='now', scope='node', page='voice', section='Sessions and limits', label='Model hold duration', unit='s', control='number', min=0)
_add('voice', 'hold_every', 'float', tier='now', scope='node', page='voice', section='Sessions and limits', label='Renew model hold every', unit='s', control='number', min=0)
_add('voice', 'hold_after_last_call', 'float', tier='now', scope='node', page='voice', section='Sessions and limits', label='Hold after the last call', unit='s', control='number', min=0)
_add('voice', 'model_parallel', 'int', tier='restart', scope='node', page='voice', section='Sessions and limits', label='Model request capacity', control='table', min=0)
_add('voice', 'speech_threshold', 'float', tier='now', scope='node', page='voice', section='Speech', label='Speech detection threshold', control='number', min=0)
_add('voice', 'profiles', 'table', tier='now', scope='node', page='machine', section='Additional settings', label='Profiles')
_add('voice', 'greetings', 'table', tier='now', scope='node', page='machine', section='Additional settings', label='Greetings', help='Short prerecorded phrases; changes regenerate the complete set and refresh the control node.', rule=_greetings)
_add('voice', 'observability_enabled', 'bool', tier='file', scope='node', page='machine', section='Additional settings', label='Observability enabled')
_add('voice', 'observability_export_url', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Observability export url')

# turnstile
_add('turnstile', 'host', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Host')
_add('turnstile', 'ports', 'list', tier='file', scope='front', page='machine', section='Additional settings', label='Ports')
_add('turnstile', 'keep_console_settings_on_front', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Keep console settings on front', rule=_console_owner)
_add('turnstile', 'toollocker_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Toollocker url')
_add('turnstile', 'response_max_tool_calls', 'int', tier='now', scope='front', page='tools', section='Tools', label='Tool calls per turn', control='number', min=1, max=10000)
_add('turnstile', 'response_max_tool_seconds', 'float', tier='now', scope='front', page='tools', section='Tools', label='Tool turn time limit', unit='s', control='number', min=0, max=86400, exclusive_min=True)
_add('turnstile', 'response_keep_days', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Stored Responses retention', unit='days', control='number', min=0, max=3650, exclusive_min=True)
_add('turnstile', 'wait', 'float', tier='restart', scope='front', page='requests', section='Requests and queue', label='Wait for a node', unit='s', control='number', min=0, exclusive_min=True)
_add('turnstile', 'allow_caller_to_specify_destination', 'bool', tier='now', scope='front', page='requests', section='Requests and queue', label='Allow callers to choose the node', control='switch')
_add('turnstile', 'preferred_voice_node', 'text', tier='now', scope='front', page='voice', section='Speech', label='Preferred voice node', control='text', choices_from='voice_nodes')
_add('turnstile', 'artifact_machine', 'text', tier='restart', scope='front', page='general', section='Model files', label='File download node', control='text')
_add('turnstile', 'image_adult_previews', 'bool', tier='now', scope='front', page='machine', section='Additional settings', label='Image adult previews')
_add('turnstile', 'files_every', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Check model files every', unit='min', control='number', min=0)
_add('turnstile', 'component_versions_every', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Component version check interval', unit='min', control='number', min=0, rule=_component_interval)
_add('turnstile', 'keep_days', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Event log retention', unit='days', control='number', min=0, help='Days the control node retains the suite event log. Applies immediately to pruning; 0 keeps everything.')
_add('turnstile', 'compute', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Compute')
_add('turnstile', 'data', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Data')
_add('turnstile', 'wake_mac', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake mac')
_add('turnstile', 'wake_address', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake address')
_add('turnstile', 'max_in_flight', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Max in flight', min=1)
_add('turnstile', 'busy_retry_seconds', 'float', tier='file', scope='front', page='machine', section='Additional settings', label='Busy retry seconds', min=0, exclusive_min=True)
_add('turnstile', 'hosted_keys', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Hosted keys')
_add('turnstile', 'machines', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Machines')
_add('turnstile', 'machines_poll_seconds', 'float', tier='file', scope='front', page='machine', section='Additional settings', label='Machines poll seconds', min=0, exclusive_min=True)
_add('turnstile', 'voice_port', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Voice port', min=0)
_add('turnstile', 'machine_key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Machine key file')
_add('turnstile', 'plugins_dir', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Plugins dir')
_add('turnstile', 'auth_key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Auth key file')
_add('turnstile', 'databases', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Databases')
_add('turnstile', 'groups', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Groups')
_add('turnstile', 'models_folder', 'text', tier='now', scope='front', page='general', section='Model files', label='Active model cache', control='text', rule=_absolute)
_add('turnstile', 'models_storage_folder', 'text', tier='now', scope='front', page='general', section='Model files', label='Model storage library', control='text', rule=_absolute)
_add('turnstile', 'allow_image_generation', 'bool', tier='file', scope='front', page='machine', section='Platform services', label='Image generation', control='switch')
_add('turnstile', 'allow_speech', 'bool', tier='file', scope='front', page='machine', section='Platform services', label='Speech · TTS, STT and Voice', control='switch')
_add('turnstile', 'allow_audio_generation', 'bool', tier='file', scope='front', page='machine', section='Platform services', label='Music and audio generation', control='switch')
_add('turnstile', 'allow_model_downloads', 'bool', tier='file', scope='front', page='machine', section='Platform services', label='Model downloads', control='switch')
_add('turnstile', 'allow_classifiers', 'bool', tier='file', scope='front', page='machine', section='Platform services', label='Classifiers', control='switch')
_add('turnstile', 'compute_offset', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Compute offset', derived=True)

# weightroom
_add('weightroom', 'host', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Host', help='Address and port weightroom listens on. 0.0.0.0 means every address this machine has, which is what lets it be reached on the home network and through Tailscale alike.')
_add('weightroom', 'port', 'int', tier='file', scope='front', page='machine', section='Additional settings', label='Port', min=0)
_add('weightroom', 'key_file', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Key file', help='The file that holds weightroom\'s own key at LoadCoach, and nothing else. Make the key on compute, in the group "system", and give the account the two things weightroom needs: loadcoach user add weightroom system loadcoach user adjust weightroom add=sign-in,mirror-users Put what it prints in this file, and make the file readable by you alone: chmod 600')
_add('weightroom', 'data', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Data', help='The folder for the little that weightroom keeps: the last status it saw of compute, and a copy of the password hashes of the few people who may act while compute is off. Saved web-chat conversations and attachments live here in chat.db. Benchmark definitions, queue and results live here too, in benchmarks.db. Operational alert history and acknowledgement live in alerts.db. Recurring schedule definitions and occurrence history live in schedules.db.')
_add('weightroom', 'plugins_dir', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Plugins dir', help='Root folder of discoverable external-service plugins. WeightRoom and turnstile share their plugin-local settings.json files; pairing keys and authorization snapshots stay in keys/data.')
_add('weightroom', 'voice_gateway', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Voice gateway', help='Private voice-gateway listener for System → Voice status, recording and synthesis controls. Do not use its public Twilio or desktop port. Operator sessions are checked by turnstile.')
_add('weightroom', 'audio_lyrics_model', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Audio lyrics model', help='SoundRoom audio pages (Chat → Music, Sounds and Speech). The chat model or role that writes a style caption and lyrics on the Music page, and the model each page selects first; empty selects the first audio model of that kind the person may use. Changes apply immediately to span writers and pruning.')
_add('weightroom', 'audio_music_model', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Audio music model')
_add('weightroom', 'audio_sound_model', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Audio sound model')
_add('weightroom', 'audio_speech_model', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Audio speech model')
_add('weightroom', 'audio_transcription_model', 'text', tier='restart', scope='front', page='machine', section='Additional settings', label='Audio transcription model')
_add('weightroom', 'toollocker', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Toollocker', help="ToolLocker's base URL for the System > Tools console. Empty uses turnstile on this machine, port 8443. Tool calls use the signed-in person's session key.")
_add('weightroom', 'timezone', 'text', tier='restart', scope='front', page='general', section='Display and costs', label='Time zone', control='text', help="The zone that times are shown in. Left out, it is this machine's own, which may not be yours.", rule=_timezone)
_add('weightroom', 'allow_image_generation', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Image generation', control='switch', help='Allow image generation and its model setup throughout the platform. Changes apply immediately.')
_add('weightroom', 'allow_speech', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Speech · TTS, STT and Voice', control='switch', help='Allow text-to-speech, speech-to-text and Voice. Changes apply immediately.')
_add('weightroom', 'allow_audio_generation', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Music and audio generation', control='switch', help='Allow music and sound generation. Speech has its own switch. Changes apply immediately.')
_add('weightroom', 'allow_model_downloads', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Model downloads', control='switch', help='Allow model discovery and downloads. Civitai also requires image generation. Changes apply immediately.')
_add('weightroom', 'allow_classifiers', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Classifiers', control='switch')
_add('weightroom', 'mock_input_rate', 'float', tier='restart', scope='front', page='general', section='Display and costs', label='Input token estimate', unit='$ / M tokens', control='number', min=0, max=1000000, help='USD per million input tokens used to estimate cost when a request has no recorded cost and its model has no known rate. This is the starting value in the Usage and Home pages.')
_add('weightroom', 'mock_output_rate', 'float', tier='restart', scope='front', page='general', section='Display and costs', label='Output token estimate', unit='$ / M tokens', control='number', min=0, max=1000000, help='USD per million output tokens used for that same estimate.')
_add('weightroom', 'third_party_services', 'list', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Third-party services to track', control='services', help='Optional system services shown on Machine and offered as sources in System Logs. The settings page presents the supported names as checkboxes. Ollama and Docker preserve the existing view; Apache and Nginx can be enabled when this machine runs them.', rule=_services)
_add('weightroom', 'backup_retention', 'int', tier='now', scope='front', page='retention', section='Logs and retention', label='Backups to keep', control='number', min=0, max=100, help='Number of verified recovery sets kept on each machine. Zero keeps every set. Change this on System > Backups; rotation happens after the next successful manual backup.')
_add('weightroom', 'alert_gpu_temperature', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='GPU temperature alert', unit='°C', control='number', min=50, max=110, help='GPU temperature alert threshold in degrees Celsius, from 50 through 110. This is a notification threshold, not a thermal shutdown control. Changed live in System > Settings.')
_add('weightroom', 'observation_error_percent', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Request error threshold', unit='%', control='number', min=0, help='Observation alerts: enable rate/latency thresholds after measuring a baseline.', rule=_alert('observation_error_percent'))
_add('weightroom', 'observation_p95_seconds', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Request latency threshold', unit='s', control='number', min=0, rule=_alert('observation_p95_seconds'))
_add('weightroom', 'observation_min_samples', 'int', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Minimum request samples', control='number', min=0, rule=_alert('observation_min_samples'))
_add('weightroom', 'observation_window_seconds', 'int', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Observation window', unit='s', control='number', min=0, rule=_alert('observation_window_seconds'))
_add('weightroom', 'observation_alert_hold_seconds', 'int', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Alert hold time', unit='s', control='number', min=0, rule=_alert('observation_alert_hold_seconds'))
_add('weightroom', 'observation_keep_days', 'int', tier='now', scope='front', page='retention', section='Logs and retention', label='Span retention', min=1, help='Observation spans kept on the control node: days kept (1-30), the percent of all-successful traces kept (0-100; spans that did not succeed are always kept, and every span still counts in the metrics), and a cap on stored spans (0 is none). Polling makes most spans, so a low percent saves the most. Changes apply immediately to span writers and pruning.', rule=_policy('observation_keep_days'), unit='days', control='number', max=30)
_add('weightroom', 'observation_success_percent', 'int', tier='now', scope='front', page='retention', section='Logs and retention', label='Successful traces kept', min=0, rule=_policy('observation_success_percent'), unit='%', control='number', max=100)
_add('weightroom', 'observation_max_spans', 'int', tier='now', scope='front', page='retention', section='Logs and retention', label='Span count cap', min=0, rule=_policy('observation_max_spans'), unit='spans', control='number', max=100000000)
_add('weightroom', 'observation_quiet_routes', 'list', tier='now', scope='front', page='retention', section='Logs and retention', label='Quiet routes', help='Routine polls: a successful trace that starts at one of these routes is not kept (its failures are, and it still counts in the metrics). A poll made inside other work, such as a routing lookup during a chat request, does not count. [] keeps every poll. Turnstile records these exact paths instead of its catch-all template only for the routes listed in loadcoach/observability.py POLLS.', rule=_policy('observation_quiet_routes'), unit='', control='table')
_add('weightroom', 'groups', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Groups', help='Whoever owns the machine. The first account should be in this group: weightroom user add NAME superuser')
_add('weightroom', 'compute', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Compute')
_add('weightroom', 'door', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Door')
_add('weightroom', 'secure', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Secure')
_add('weightroom', 'spotter', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Spotter')
_add('weightroom', 'chat_model_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Chat model url')
_add('weightroom', 'toollocker_runs_on_front', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Toollocker runs on front')
_add('weightroom', 'databases', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Databases')
_add('weightroom', 'wake_mac', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake mac')
_add('weightroom', 'wake_address', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake address')
_add('weightroom', 'machines', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Machines')
_add('weightroom', 'turnstile', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Turnstile', help="turnstile's own part. Seconds it waits for compute to wake before it gives up on a request.")
_add('weightroom', 'origins', 'list', tier='file', scope='front', page='machine', section='Additional settings', label='Origins')
_add("voice", 'profiles.default.model', 'text', section='Direct conversations', label='Model', unit='', control='text', page="voice")
_add("voice", 'profiles.default.system_prompt', 'text', section='Direct conversations', label='Instructions', unit='', control='text', page="voice")
_add("voice", 'profiles.default.modes', 'list', section='Direct conversations', label='Conversation modes', unit='', control='text', page="voice")
_add("voice", 'profiles.default.max_output_tokens', 'int', section='Direct conversations', label='Output limit', unit='tokens', control='number', page="voice")
_add("voice", 'profiles.default.max_context_tokens', 'int', section='Direct conversations', label='Context limit', unit='tokens', control='number', page="voice")
_add("voice", 'profiles.default.speaker', 'text', section='Speech', label='Speaker', unit='', control='text', page="voice")
