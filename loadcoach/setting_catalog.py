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
    default: object = None
    choice_labels: tuple = ()
    credential_file: str = ""
    credential_default: str = ""
    permission: str = 'manage-settings'

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
        if self.ceiling is not None and not (context or {}).get('superuser_limits') and value > self.ceiling:
            raise ValueError(f'{self.name} must be at most {self.ceiling}.')
        if self.choices and value not in self.choices:
            raise ValueError(f'{self.name}: choose one of {", ".join(map(str, self.choices))}.')
        if self.choices_from and context and self.choices_from in context:
            if value and value not in context[self.choices_from]():
                raise ValueError(f'{self.name}: choose an available value, or Automatic.')
        if self.rule:
            self.rule(value)
        return value


def _timezone(value):
    """Validate the timezone setting before saving it."""
    try:
        if value:
            ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('timezone must name an available time zone, or be empty.') from None


def _absolute(value):
    """Validate the absolute setting before saving it."""
    if value and not value.startswith('/'):
        raise ValueError('Model folders must be a full path, or empty.')


def _distinct(value):
    """Validate the distinct setting before saving it."""
    if any(not isinstance(one, str) or not one for one in value) or len(value) != len(set(value)):
        raise ValueError('Use a list of distinct names.')


def _services(value):
    """Validate the services setting before saving it."""
    _distinct(value)
    if set(value) - {'ollama', 'docker', 'apache', 'nginx'}:
        raise ValueError('third_party_services may contain ollama, docker, apache and nginx once each.')


def _engine_gpus(value):
    """Validate the engine gpus setting before saving it."""
    from engines import engine_for
    for name, cards in value.items():
        engine_for(name)
        if not isinstance(cards, list):
            raise ValueError('engine_gpus is a table of engine IDs and card lists.')
        _distinct(cards)


def _engines(value):
    """Validate the engines setting before saving it."""
    from engines import checked_installations
    checked_installations(value, value.get('llama_cpp', {}).get('executable', 'llama-server'))


def _component_interval(value):
    """Validate the component interval setting before saving it."""
    from loadcoach.component_versions import interval_seconds
    interval_seconds(value)


def _greetings(value):
    """Validate the greetings setting before saving it."""
    from voice.prompt_recordings import texts
    texts({'greetings': value})


def _telephone(value):
    """Validate the telephone setting before saving it."""
    from voice.telephone_settings import validate
    validate(value)


def _console_owner(value):
    """Validate the console owner setting before saving it."""
    if not value:
        raise ValueError('keep_console_settings_on_front=false is no longer supported; the control node stores its console settings.')


def _toolsets(value):
    """Validate the toolsets setting before saving it."""
    for name, toolset in value.items():
        tools = toolset.get('tools') if isinstance(toolset, dict) and toolset.keys() == {'tools'} else None
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError('toolsets needs lowercase collection names and tools, a list of names.')


def _tool_levels(value):
    """Validate the tool levels setting before saving it."""
    from loadcoach.groups import LEVELS
    if not all(isinstance(tool, str) and level in LEVELS for tool, level in value.items()):
        raise ValueError(f'tool_levels gives each tool one of {", ".join(LEVELS)}.')


def validate_patch(program, current, patch, *, context=None):
    """Validate a whole POST before writing, including table relationships and resets."""
    effective = {**current, **{name: value for name, value in patch.items() if value is not None}}
    for name, value in patch.items():
        if value is not None:
            validate(program, name, value, context=context)
            from loadcoach.settings import redacted
            entry = get(program, name)
            if entry and (entry.kind == 'secret' or redacted(value, name) != value or '(not shown)' in str(value)):
                raise ValueError(f'{name}: secrets require their write-only credential form.')
        elif (entry := get(program, name)) and entry.kind == 'secret':
            raise ValueError(f'{name}: secrets require their write-only credential form.')
    if program == 'turnstile' and {'models_folder', 'models_storage_folder'} & patch.keys():
        from turnstile.artifacts import checked_roots
        checked_roots({name: effective.get(name, '') for name in ('models_folder', 'models_storage_folder')})
    return effective


def _policy(name):
    """Build a validator against the shared observation retention policy."""
    def check(value):
        """Validate this value through its shared observation contract."""
        from loadcoach.observation_store import POLICY, check_policy
        values = {**POLICY, name.removeprefix('observation_'): value}
        check_policy(**values)
    return check


def _alert(name):
    """Build a validator against the named observation alert contract."""
    def check(value):
        """Validate this value through its shared observation contract."""
        from loadcoach.observability import alert_value
        alert_value(name, value)
    return check


def _wake_mac(value):
    """Validate the wake mac setting before saving it."""
    import re
    if value and not re.fullmatch(r'(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}|[0-9a-fA-F]{12}', value):
        raise ValueError('Wake MAC must be a hardware address, or empty to disable Wake.')


def _wake_address(value):
    """Validate the wake address setting before saving it."""
    from ipaddress import IPv4Address
    try:
        IPv4Address(value)
    except ValueError:
        raise ValueError('Wake address must be an IPv4 address.') from None


def _http_url(value):
    """Validate the http url setting before saving it."""
    from urllib.parse import urlsplit
    try:
        url = urlsplit(value)
        if value and (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment):
            raise ValueError()
        url.port
    except ValueError:
        raise ValueError('Use an HTTP or HTTPS URL without embedded credentials or a fragment.') from None


def _hermes_tools(value):
    """Validate the hermes tools setting before saving it."""
    import re
    _distinct(value)
    if len(value) > 64 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name) for name in value):
        raise ValueError('Use at most 64 toolset names containing letters, digits, dashes or underscores.')


def _disks(value):
    """Validate the disks setting before saving it."""
    if len(value) > 32 or any(not isinstance(path, str) or not path.startswith('/') or '\0' in path for path in value):
        raise ValueError('disks must contain at most 32 absolute paths.')


def _proxy_networks(value):
    """Validate the proxy networks setting before saving it."""
    from ipaddress import ip_network
    if len(value) > 128 or any(not isinstance(item, str) for item in value):
        raise ValueError('trusted_proxies accepts at most 128 IP addresses or CIDR networks.')
    try:
        for item in value:
            ip_network(item, strict=False)
    except ValueError:
        raise ValueError('trusted_proxies must contain IP addresses or CIDR networks.') from None


# Each service's entries are explicit: adding a setting here is the only way to
# grant site editability. No file supplied by an operator changes this catalogue.
CATALOG: dict[tuple[str, str], Setting] = {}


def _add(program, name, kind, **metadata):
    """Register one setting contract, rejecting duplicate program and key pairs."""
    entry = Setting(program, name, kind, **metadata)
    if (program, name) in CATALOG:
        raise ValueError(f'Duplicate setting: {program}.{name}')
    CATALOG[program, name] = entry


def get(program, name):
    """Find a contract, including machine-owned per-node override namespaces."""
    return CATALOG.get(('machines' if program.startswith('machines:') else program, name))


def entries(program=None):
    """Return catalogue entries, optionally limited to one program."""
    return tuple(value for value in CATALOG.values() if program is None or value.program == program)


def default_for(program, name):
    """A fresh catalogue default for service constructors and point-of-use readers."""
    from copy import deepcopy
    return deepcopy(get(program, name).default)


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
    for entry in entries(program):
        if '.' in entry.name:
            value = values
            for part in entry.name.split('.'):
                if not isinstance(value, dict) or part not in value:
                    break
                value = value[part]
            else:
                entry.check(value, context=context)
    return values


def changeable():
    """Group non-retired editable settings by their owning program and change tier."""
    result = {}
    for setting in CATALOG.values():
        if setting.scope in ('node','front') and setting.tier != FILE and not (setting.retired or setting.derived):
            result.setdefault(setting.program, {})[setting.name] = setting.tier
    return result


def retired_for(program, name, context=None):
    """A conditional retirement must not retire the same key on standalone nodes."""
    entry = get(program, name)
    return bool(entry and (entry.retired or entry.retired_when and (context or {}).get(entry.retired_when)))


def changeable_for(program, context=None):
    """Return settings editable in the supplied program and runtime context."""
    return {entry.name: entry.tier for entry in entries(program)
            if entry.scope in ('node','front') and entry.tier != FILE and not entry.derived and not retired_for(program, entry.name, context)}


# Existing service settings.

# loadcoach
_add('loadcoach', 'host', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Host', help='Address and port LoadCoach listens on. Loopback means this machine only.')
_add('loadcoach', 'port', 'int', tier='file', scope='node', page='machine', section='Additional settings', label='Port', min=0)
_add('loadcoach', 'data', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Data', help='The folder LoadCoach keeps its database and the token for the `loadcoach` command in, counted from this file. Left out, they are kept beside this file.')
_add('loadcoach', 'machine_key_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine key file', help="The key this machine shares with the control node, counted from this file. Turnstile signs what it sends on with it, and LoadCoach, the ToolLocker, spotter and voice believe nothing else about who is asking. Readable by you alone, and the same on both machines: python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ../keys/machine.key && chmod 600 ../keys/machine.key Left out, nobody but this machine itself may ask, as anyone: only for a host of 127.0.0.1.")
_add('loadcoach', 'machine_name', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Machine name', help='This instance\'s stable name in the control node\'s [machines.NAME] registry. The primary is "compute"; a second instance might be "rig". Give every instance its own key, data folder and port.')
_add('loadcoach', 'front', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Control node URL', help="Turnstile's address on the control node. Compute asks it for the accounts, for work done later in someone's name: a batch, a benchmark task, a recurring schedule, and for the reports.")
_add('loadcoach', 'llama_server', 'text', tier='restart', scope='node', page='machine', section='Additional settings', label='Llama server', help="Compatibility path for llama.cpp. A service does not always have your shell's PATH. Prefer [engines.llama_cpp] below for new configuration; if both paths exist they must match.", deprecated=True)
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
_add('loadcoach', 'unlogged', 'bool', tier='now', scope='node', page='security', section='Security and sign-in', label='Allow requests without logs', control='switch', help='Whether a request may be sent that leaves nothing behind: no entry in the event log, no request number, no kept text. false, as here, refuses every such request. With true, an account that has the ability "unlogged" may send one, by adding this header to it: X-LoadCoach-Log: off Such a request is not counted in anyone\'s usage, cannot be marked sensitive or given a piece of work, and cannot go to a hosted model. Asking for one without the ability is refused, and that refusal is put on record.')
_add('loadcoach', 'model_defaults', 'table', tier='now', scope='node', page='nodes', section='Advanced', label='Inherited loading flags', control='table', help='Flags that every model gets, unless its own section sets a different value for the same flag. These three shrink the memory a model\'s context takes to about half, measured here at 53 %, by keeping it at 8 bits a number, not 16. The effect on answers is usually too small to notice. A model that should keep full precision sets cache-type-k and cache-type-v to "f16". "q4_0" halves it again, to 28 %, at a cost that can be noticed.')
_add('loadcoach', 'engines', 'table', tier='restart', scope='node', page='machine', section='Additional settings', label='Engines', help='Per-node inference-engine overrides, edited under System > Compute nodes. Empty preserves llama.cpp enabled with the legacy llama_server path. Executable/enablement changes require a node restart; saving never installs programs or interrupts loaded models.', rule=_engines)
_add('loadcoach', 'models', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Models')
_add('loadcoach', 'places', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Places')
_add('loadcoach', 'hub_token_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Hub token file')
_add('loadcoach', 'civitai_token_file', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Civitai token file')
_add('loadcoach', 'gpus', 'list', tier='file', scope='node', page='machine', section='Additional settings', label='Gpus')
_add('loadcoach', 'disabled_gpus', 'list', tier='now', scope='node', page='machine', section='Additional settings', label='Disabled gpus', rule=_distinct)
_add('loadcoach', 'former_machine_name', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Former machine name')
_add('loadcoach', 'toollocker_runs_on_front', 'bool', tier='file', scope='node', page='machine', section='Additional settings', label='ToolLocker runs on control node')
_add('loadcoach', 'databases', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Databases')
_add('loadcoach', 'nvidia_smi', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Nvidia smi')

# spotter
_add('spotter', 'host', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Host', help="Address and port spotter listens on. It is asked by the control node, so give compute's own address.")
_add('spotter', 'port', 'int', tier='file', scope='node', page='machine', section='Additional settings', label='Port', min=0)
_add('spotter', 'data', 'text', tier='file', scope='node', page='machine', section='Additional settings', label='Data', help='The folder spotter keeps its readings in, counted from this file: spotter.toml gives spotter.db there. Left out, beside this file.')
_add('spotter', 'disks', 'list', tier='now', scope='node', page='nodes', section='Telemetry and power', label='Watched disk paths', help='Folders whose drives are watched for room.', rule=_disks)
_add('spotter', 'services', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='Services', help='The services spotter may start, stop and restart: a name of your choosing = the unit it is. Units of this account, the ones `systemctl --user` shows. Nothing that is not listed here or below can be touched, whatever is asked.')
_add('spotter', 'system_services', 'table', tier='file', scope='node', page='machine', section='Additional settings', label='System services', help="The same for root's units. Listing one here is not enough: this account must also be allowed to act on it, by the polkit rule in services/spotter.rules, which names the same units and which root installs. Without the rule, the buttons are there and systemd refuses.")
_add('spotter', 'sample_every', 'float', tier='now', scope='node', page='alerts', section='Alerts and monitoring', label='Sampling interval', unit='s', control='number', min=1, help='Seconds between the readings that are kept, and the days they are kept for. A reading is about 400 bytes: every 30 seconds for 28 days is 37 MB.')
_add('spotter', 'keep_days', 'float', tier='now', scope='node', page='retention', section='Logs and retention', label='Telemetry retention', unit='days', control='number', min=0, help='Days of telemetry readings kept by this node. Zero removes readings at the next daily prune. Changes apply immediately to the next prune.')
_add('spotter', 'sleep_after', 'float', tier='now', scope='node', page='nodes', section='Power and idle', label='Sleep after idle', unit='min', control='number', min=0, help='Minutes with nothing using the machine before spotter puts it to sleep. 0, or left out, means never. "Using" is any of: a request or tool call at LoadCoach, a key or the mouse touched at the machine\'s own screen, a GPU at `busy_gpu` percent or more, the processor at `busy_processor` percent or more. Someone signed in over SSH and doing nothing does not count. A job that must not be put to sleep under can say so itself: systemd-inhibit --what=sleep --why="a long job" COMMAND Before turning this on, make sure the machine can be woken again: configure Wake for this node on Compute nodes or in [machines.NAME] in settings/weightroom.toml. A machine that sleeps and cannot be woken has to be walked to.')
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
_add('turnstile', 'keep_console_settings_on_front', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Keep console settings on control node', rule=_console_owner, deprecated=True)
_add('turnstile', 'toollocker_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Toollocker url')
_add('turnstile', 'response_max_tool_calls', 'int', tier='now', scope='front', page='tools', section='Tools', label='Tool calls per turn', control='number', min=1, max=10000)
_add('turnstile', 'response_max_tool_seconds', 'float', tier='now', scope='front', page='tools', section='Tools', label='Tool turn time limit', unit='s', control='number', min=0, max=86400, exclusive_min=True)
_add('turnstile', 'response_keep_days', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Stored Responses retention', unit='days', control='number', min=0, max=3650, exclusive_min=True)
_add('turnstile', 'wait', 'float', tier='now', scope='front', page='requests', section='Requests and queue', label='Wait for a node', unit='s', control='number', min=0, exclusive_min=True)
_add('turnstile', 'allow_caller_to_specify_destination', 'bool', tier='now', scope='front', page='requests', section='Requests and queue', label='Allow callers to choose the node', control='switch')
_add('turnstile', 'preferred_voice_node', 'text', tier='now', scope='front', page='voice', section='Speech', label='Preferred voice node', control='text', choices_from='voice_nodes')
_add('turnstile', 'artifact_machine', 'text', tier='restart', scope='front', page='general', section='Model files', label='File download node', control='text')
_add('turnstile', 'image_adult_previews', 'bool', tier='now', scope='front', page='machine', section='Additional settings', label='Image adult previews')
_add('turnstile', 'files_every', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Check model files every', unit='min', control='number', min=0)
_add('turnstile', 'component_versions_every', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Component version check interval', unit='min', control='number', min=0, rule=_component_interval)
_add('turnstile', 'keep_days', 'float', tier='now', scope='front', page='retention', section='Logs and retention', label='Event log retention', unit='days', control='number', min=0, help='Days the control node retains the suite event log. Applies immediately to pruning; 0 keeps everything.')
_add('turnstile', 'compute', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Compute')
_add('turnstile', 'data', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Data')
_add('turnstile', 'wake_mac', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake mac', deprecated=True)
_add('turnstile', 'wake_address', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake address', deprecated=True)
_add('turnstile', 'max_in_flight', 'int', tier='restart', scope='front', page='requests', section='Requests and queue', label='Maximum in-flight requests', min=1, unit='', control='number')
_add('turnstile', 'busy_retry_seconds', 'float', tier='now', scope='front', page='requests', section='Requests and queue', label='Busy retry delay', min=0, exclusive_min=True, unit='s', control='number')
_add('turnstile', 'hosted_keys', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Hosted keys')
_add('turnstile', 'machines', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Machines')
_add('turnstile', 'machines_poll_seconds', 'float', tier='now', scope='front', page='requests', section='Requests and queue', label='Node polling interval', min=2, unit='s', control='number', max=300)
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
_add('weightroom', 'audio_lyrics_model', 'text', tier='now', scope='front', page='audio', section='Audio defaults', label='Lyrics helper model', help='A chat model or role, such as chat, that writes the style caption and lyrics.', control='text')
_add('weightroom', 'audio_music_model', 'text', tier='now', scope='front', page='audio', section='Audio defaults', label='Music page model', help='Selected first on Interact → Music; empty selects the first available.', control='text')
_add('weightroom', 'audio_sound_model', 'text', tier='now', scope='front', page='audio', section='Audio defaults', label='Sounds page model', help='Selected first on Interact → Sounds.', control='text')
_add('weightroom', 'audio_speech_model', 'text', tier='now', scope='front', page='audio', section='Audio defaults', label='Voice generation model', help='Selected first on Interact → Voice generation.', control='text')
_add('weightroom', 'audio_transcription_model', 'text', tier='now', scope='front', page='audio', section='Audio defaults', label='Transcription model', help='Selected first on Interact → Transcribe.', control='text')
_add('weightroom', 'toollocker', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Toollocker', help="ToolLocker's base URL for the System > Tools console. Empty uses turnstile on this machine, port 8443. Tool calls use the signed-in person's session key.")
_add('weightroom', 'timezone', 'text', tier='now', scope='front', page='general', section='Display and costs', label='Time zone', control='text', help="The zone that times are shown in. Left out, it is this machine's own, which may not be yours.", rule=_timezone)
_add('weightroom', 'allow_image_generation', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Image generation', control='switch', help='Allow image generation and its model setup throughout the platform. Changes apply immediately.')
_add('weightroom', 'allow_speech', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Speech · TTS, STT and Voice', control='switch', help='Allow text-to-speech, speech-to-text and Voice. Changes apply immediately.')
_add('weightroom', 'allow_audio_generation', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Music and audio generation', control='switch', help='Allow music and sound generation. Speech has its own switch. Changes apply immediately.')
_add('weightroom', 'allow_model_downloads', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Model downloads', control='switch', help='Allow model discovery and downloads. Civitai also requires image generation. Changes apply immediately.')
_add('weightroom', 'allow_classifiers', 'bool', tier='now', scope='front', page='general', section='Platform services', label='Classifiers', control='switch')
_add('weightroom', 'mock_input_rate', 'float', tier='now', scope='front', page='general', section='Display and costs', label='Input token estimate', unit='$ / M tokens', control='number', min=0, max=1000000, help='USD per million input tokens used to estimate cost when a request has no recorded cost and its model has no known rate. This is the starting value in the Usage and Home pages.')
_add('weightroom', 'mock_output_rate', 'float', tier='now', scope='front', page='general', section='Display and costs', label='Output token estimate', unit='$ / M tokens', control='number', min=0, max=1000000, help='USD per million output tokens used for that same estimate.')
_add('weightroom', 'third_party_services', 'list', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='Third-party services to track', control='services', help='Optional system services shown on Machine and offered as sources in System Logs. The settings page presents the supported names as checkboxes. Ollama and Docker preserve the existing view; Apache and Nginx can be enabled when this machine runs them.', rule=_services)
_add('weightroom', 'backup_retention', 'int', tier='now', scope='front', page='retention', section='Logs and retention', label='Backups to keep', control='number', min=0, max=100, help='Number of verified recovery sets kept on each machine. Zero keeps every set. Change this on System > Backups; rotation happens after the next successful manual or scheduled backup.')
_add('weightroom', 'alert_gpu_temperature', 'float', tier='now', scope='front', page='alerts', section='Alerts and monitoring', label='GPU temperature alert', unit='°C', control='number', min=50, max=110, help='GPU temperature alert threshold in degrees Celsius, from 50 through 110. This is a notification threshold, not a thermal shutdown control. Changes apply immediately from Settings > Alerts and notifications.')
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
_add('weightroom', 'compute', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Compute', retired=True)
_add('weightroom', 'door', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Door', derived=True)
_add('weightroom', 'secure', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='Secure')
_add('weightroom', 'spotter', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Spotter', retired=True)
_add('weightroom', 'chat_model_url', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Chat model url')
_add('weightroom', 'toollocker_runs_on_front', 'bool', tier='file', scope='front', page='machine', section='Additional settings', label='ToolLocker runs on control node')
_add('weightroom', 'databases', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Databases')
_add('weightroom', 'wake_mac', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake mac', deprecated=True)
_add('weightroom', 'wake_address', 'text', tier='file', scope='front', page='machine', section='Additional settings', label='Wake address', deprecated=True)
_add('weightroom', 'machines', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Machines')
_add('weightroom', 'turnstile', 'table', tier='file', scope='front', page='machine', section='Additional settings', label='Turnstile', help="turnstile's own part. Seconds it waits for compute to wake before it gives up on a request.")
_add('weightroom', 'origins', 'list', tier='file', scope='front', page='machine', section='Additional settings', label='Origins')
_add("voice", 'profiles.default.model', 'text', section='Direct conversations', label='Model', unit='', control='text', page="voice")
_add("voice", 'profiles.default.system_prompt', 'text', section='Direct conversations', label='Instructions', unit='', control='text', page="voice")
_add("voice", 'profiles.default.modes', 'list', section='Direct conversations', label='Conversation modes', unit='', control='text', page="voice")
_add("voice", 'profiles.default.max_output_tokens', 'int', section='Direct conversations', label='Output limit', unit='tokens', control='number', page="voice")
_add("voice", 'profiles.default.max_context_tokens', 'int', section='Direct conversations', label='Context limit', unit='tokens', control='number', page="voice")
_add("voice", 'profiles.default.speaker', 'int', section='Speech', label='Speaker', unit='', control='text', page="voice")

_add('machines', 'wake_mac', 'text', tier=NOW, scope='front', page='nodes', section='Wake', label='Wake MAC address', default='', control='plain-text', rule=_wake_mac, help='This node’s hardware address. Empty disables Wake. Applies immediately.')
_add('machines', 'wake_address', 'text', tier=NOW, scope='front', page='nodes', section='Wake', label='Wake broadcast address', default='255.255.255.255', control='plain-text', rule=_wake_address, help='IPv4 destination for Wake packets. Applies immediately; the file value is used after reset.')

_add('voice', 'recognition.threads', 'int', tier=RESTART, page='voice', section='Recognition', label='Recognition threads', control='number', min=1, max=64, default=2)
_add('voice', 'recognition.decoding_method', 'choice', tier=RESTART, page='voice', section='Recognition', label='Decoding method', choices=('greedy_search', 'modified_beam_search'), default='modified_beam_search')
_add('voice', 'recognition.max_active_paths', 'int', tier=RESTART, page='voice', section='Recognition', label='Active recognition paths', control='number', min=1, max=64, default=8)
_add('voice', 'recognition.trailing_silence_ms', 'int', tier=NOW, page='voice', section='Recognition', label='End-of-turn silence', control='number', min=100, max=10000, unit='ms', default=800, help='Applies to new voice sessions; existing sessions keep their endpoint settings.')
_add('voice', 'recognition.max_utterance_seconds', 'float', tier=NOW, page='voice', section='Recognition', label='Maximum utterance', control='number', min=1, max=300, unit='s', default=30, help='Applies to new voice sessions.')
_add('voice', 'synthesis.speaker', 'int', tier=RESTART, page='voice', section='Synthesis', label='Default synthesis speaker', control='number', min=0, max=1023, default=0)
_add('voice', 'synthesis.threads', 'int', tier=RESTART, page='voice', section='Synthesis', label='Synthesis threads', control='number', min=1, max=64, default=2)
_add('voice', 'synthesis.workers', 'int', tier=RESTART, page='voice', section='Synthesis', label='Synthesis workers', control='number', min=1, max=16, default=2)

_add('voice-gateway', 'hermes.toolsets', 'list', tier=NOW, scope='front', page='voice', section='Hermes', label='Hermes toolsets', rule=_hermes_tools, default=['search'], help='Applies to new calls; active calls keep their tool choices.')
_add('voice-gateway', 'hermes.search_url', 'text', tier=NOW, scope='front', page='voice', section='Hermes', label='Hermes search URL', control='plain-text', rule=_http_url, default='', help='HTTP or HTTPS search service. Applies to new calls.')

for name, kind, value, minimum, maximum, label, unit, help_text in (
    ('sign_in_max_failures','int',5,1,100,'Sign-in failures before lockout','', 'Failures per account and client address. The shared address limit is four times this value. Applies immediately.'),
    ('sign_in_window_minutes','int',15,1,1440,'Sign-in failure window','min', 'Only failures within this rolling window count toward a lock. Applies immediately.'),
    ('sign_in_lockout_minutes','int',30,1,10080,'Sign-in lockout duration','min', 'Locked attempts are refused before password hashing. Applies to new lockouts immediately.'),
    ('password_min_length','int',8,8,128,'Minimum password length','characters', 'Applies to new or changed passwords; existing passwords keep working.'),
    ('api_key_max_days','int',0,0,36500,'Maximum API key lifetime','days', 'Zero permits keys that never expire. A positive maximum applies only to newly created keys.'),
    ('session_idle_hours','float',0,0,8760,'Idle session expiry','h', 'Zero disables idle expiry. API keys are unaffected. Active sign-ins record their last request.'),
):
    _add('turnstile',name,kind,tier=NOW,scope='front',page='security',section='Security and sign-in',
         default=value,min=minimum,max=maximum,label=label,unit=unit,control='number',help=help_text)
_add('weightroom','trusted_proxies','list',tier=FILE,scope='front',page='security',section='Connection security',
     label='Trusted proxies',default=[],rule=_proxy_networks,
     help='Set trusted proxy IP addresses or CIDR networks in settings/weightroom.toml. Forwarded client addresses are ignored unless the socket peer is trusted; only the last untrusted hop is used.')


for name, maximum, label, help_text in (
    ('max_queue',100000,'Maximum queued requests','Zero leaves the queue uncapped. Excess requests receive 503 with Retry-After; requests already admitted continue.'),
    ('max_loaded_models',1000,'Maximum loaded models','Zero leaves residency uncapped. New placements reclaim the oldest eligible idle residents; active requests, holds and model locks remain protected.'),
):
    _add('loadcoach',name,'int',tier=NOW,scope='node',page='requests',section='Requests and queue',
         label=label,min=0,max=maximum,default=0,control='number',help=help_text)

for name, kind, default, minimum, maximum, label, unit, help_text in (
    ('role_tries','int',3,1,20,'Role candidate attempts','', 'Candidate attempts for a new role request. Explicit model requests are unaffected.'),
    ('role_loaded_bonus','float',25,0,100,'Default loaded-model bonus','%', 'Used when a role has no explicit loaded bonus. Applies to new role selections.'),
    ('role_fast_loaded_bonus','float',100,0,500,'Default fast loaded-model bonus','%', 'Used when a role has no explicit fast loaded bonus. Applies to new selections.'),
    ('role_unknown_speed','float',10.0,0.01,1000000,'Unmeasured output speed','tokens/s', 'Provisional preference for legacy candidates without timing evidence. Measured request estimates remain unknown when evidence is absent.'),
    ('role_unknown_load','float',60.0,0,86400,'Unmeasured load time','s', 'Provisional preference when load timing has not been measured. Never replaces measured wait evidence.'),
    ('batch_max_lines','int',10000,1,1000000,'Maximum batch requests','', 'Checked when a new batch starts. Existing batches continue.'),
    ('batch_max_mib','float',50,1,1024,'Maximum batch upload','MiB', 'Bounds the complete multipart upload before storing it. Applies to new uploads.'),
    ('batch_tries','int',3,1,100,'Batch cut-off attempts','', 'Maximum interrupted starts of a batch line; checked before its next attempt.'),
    ('batch_retry_seconds','float',30,1,3600,'Batch retry delay','s', 'Delay after a temporary refusal. Changes interrupt the pending retry delay.'),
):
    _add('turnstile',name,kind,tier=NOW,scope='front',page='requests',section='Advanced',
         label=label,min=minimum,max=maximum,default=default,unit=unit,control='number',help=help_text)



def _notification_url(value):
    """Validate the notification url setting before saving it."""
    from urllib.parse import urlsplit
    if not value:
        return
    try:
        parts = urlsplit(value)
        valid = parts.scheme in ('http','https') and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment)
        parts.port
    except ValueError:
        valid = False
    if not valid or len(value)>2048 or any(ord(char)<=32 for char in value):
        raise ValueError('Use an HTTP or HTTPS URL without credentials, query parameters or fragments; save tokens separately.')


def _mailboxes(value):
    """Validate the mailboxes setting before saving it."""
    from email.utils import parseaddr
    if not value:
        return
    addresses = [address.strip() for address in value.split(',')]
    if len(value)>4096 or len(addresses)>20 or any(not address or '@' not in address or
            parseaddr(address)[1] != address or any(ord(char)<=32 or ord(char)>126 for char in address) for address in addresses):
        raise ValueError('Use up to 20 plain email addresses, separated by commas.')


def _notification_text(value):
    """Validate the notification text setting before saving it."""
    if len(value)>1024 or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('Use at most 1024 characters without control characters.')


def _quiet_hours(value):
    """Validate the quiet hours setting before saving it."""
    if value and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
        raise ValueError('Quiet hours use HH:MM-HH:MM in the control node time zone, or are empty.')
    if value and value[:5] == value[6:]:
        raise ValueError('Quiet hours must have different start and end times.')


def _ntfy_topic(value):
    """Validate the ntfy topic setting before saving it."""
    if value and not re.fullmatch(r'[-_A-Za-z0-9]{1,64}',value):
        raise ValueError('An ntfy topic uses 1–64 letters, numbers, underscores or dashes.')


def _mailbox(value):
    """Validate the mailbox setting before saving it."""
    _mailboxes(value)
    if ',' in value:
        raise ValueError('Use one plain email address.')


def _smtp_host(value):
    """Validate the smtp host setting before saving it."""
    if len(value)>253 or any(ord(char)<=32 or char in '/\\@?#' for char in value):
        raise ValueError('Use an SMTP server name or IP address without a port or path.')


def _notification_secret(value):
    """Validate the notification secret setting before saving it."""
    if len(value)>8192 or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('Use a credential of at most 8192 characters without control characters.')


for name, kind, default, minimum, maximum, label, unit, choices, rule, help_text in (
    ('notify_webhook_url','text','',None,None,'Webhook URL','',(),_notification_url,'JSON episode notifications with an HMAC signature. Credentials belong in the write-only signing secret.'),
    ('notify_ntfy_url','text','',None,None,'ntfy server URL','',(),_notification_url,'Server base URL; leave empty to disable this system-wide channel.'),
    ('notify_ntfy_topic','text','',None,None,'ntfy topic','',(),_ntfy_topic,'Topic for system-wide operational alerts. Optional bearer token is write-only.'),
    ('notify_smtp_host','text','',None,None,'SMTP host','',(),_smtp_host,'Mail server name or IP; leave empty to disable email delivery.'),
    ('notify_smtp_port','int',587,1,65535,'SMTP port','',(),None,'Port 587 uses STARTTLS by default; port 465 uses TLS.'),
    ('notify_smtp_security','choice','auto',None,None,'SMTP security','',('auto','starttls','tls'),None,'Automatic uses TLS on port 465 and STARTTLS elsewhere. Plaintext SMTP is not used.'),
    ('notify_smtp_from','text','',None,None,'Email sender','',(),_mailbox,'Plain sender email address.'),
    ('notify_smtp_to','text','',None,None,'Operational email recipients','',(),_mailboxes,'Plain email addresses, separated by commas.'),
    ('notify_smtp_username','text','',None,None,'SMTP username','',(),_notification_text,'Optional login name. The password is write-only; empty username uses no SMTP authentication.'),
    ('notify_site_url','text','',None,None,'Notification site URL','',(),_notification_url,'Optional public address of this console for absolute links. Empty uses relative links.'),
    ('notify_min_state','choice','opened',None,None,'Notify when','',('opened','unacknowledged'),None,'Send when an episode opens, or only when it remains unacknowledged for the configured delay. Resolutions are delivered too.'),
    ('notify_after_minutes','int',15,1,10080,'Unacknowledged delay','min',(),None,'Used only for unacknowledged delivery; acknowledgement suppresses the opening notification.'),
    ('notify_repeat_minutes','int',0,0,10080,'Repeat open episodes','min',(),None,'Zero disables repeats. Retries are limited to three attempts with backoff.'),
    ('notify_quiet_hours','text','',None,None,'Quiet hours','',(),_quiet_hours,'Empty disables quiet hours. HH:MM-HH:MM uses the control node time zone and delays pending deliveries.'),
    ('alert_disk_free_percent','float',10,0,100,'Disk free below','%',(),None,'Zero disables disk space alerts. Missing or stale readings never establish recovery.'),
    ('alert_node_offline_minutes','float',10,0,10080,'Node silent for','min',(),None,'Zero disables silence alerts. A silent node may be asleep, stopped or disconnected; this does not prove service failure.'),
    ('alert_gpu_memory_percent','float',0,0,100,'GPU memory above','%',(),None,'Zero disables GPU memory alerts. Uses fresh Spotter telemetry.'),
    ('alert_host_ram_percent','float',0,0,100,'Host RAM above','%',(),None,'Zero disables host RAM alerts. Uses fresh Spotter telemetry.'),
    ('alert_allowance_percent','float',0,0,100,'Recorded allowance above','%',(),None,'Zero disables operational allowance alerts. Unknown and in-flight costs are excluded.'),
):
    _add('weightroom',name,kind,tier=NOW,scope='front',page='alerts',section='Notification delivery' if name.startswith('notify_') else 'Alerts and monitoring',
         label=label,min=minimum,max=maximum,default=default,unit=unit,choices=choices,rule=rule,
         control='plain-text' if kind=='text' else 'number' if kind in ('int','float') else '',help=help_text)

def _bearer_secret(value):
    """Validate the bearer secret setting before saving it."""
    _notification_secret(value)
    if any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError('A bearer token must use printable ASCII without spaces.')


for name,label in (('notify_webhook_secret','Webhook signing secret'),('notify_ntfy_token','ntfy bearer token'),('notify_smtp_password','SMTP password')):
    _add('weightroom',name,'secret',tier=NOW,scope='front',page='alerts',section='Notification credentials',
         label=label,default='',rule=_bearer_secret if name=='notify_ntfy_token' else _notification_secret,help='Write-only credential kept in an owner-only file under the keys folder. Blank keeps the saved value.')

for name,kind,default,label,rule in (
    ('notify_ntfy_enabled','bool',False,'Send my notices to ntfy',None),
    ('notify_ntfy_url','text','','My ntfy server',_notification_url),
    ('notify_ntfy_topic','text','','My ntfy topic',_ntfy_topic),
    ('notify_email_enabled','bool',False,'Email my notices',None),
    ('notify_email','text','','My notification email',_mailbox),
    ('notify_operational','bool',False,'Include operational alerts',None),
):
    _add('account',name,kind,tier=NOW,scope='account',page='account',section='Notifications',label=label,default=default,rule=rule)
_add('account','notify_ntfy_token','secret',tier=NOW,scope='account',page='account',section='Notification credentials',
     label='My ntfy token',default='',rule=_bearer_secret)


# Content limits share one contract with global settings and inherited group policy.
for name, default, ceiling, unit, label, minimum in (
    ('chat_max_files', 4, 32, 'files', 'Chat attachments', 1),
    ('chat_max_file_mib', 5, 64, 'MiB', 'Each chat attachment', 1),
    ('chat_max_total_mib', 10, 256, 'MiB', 'All chat attachments', 1),
    ('chat_max_text_kib', 200, 4096, 'KiB', 'Extracted or attached text', 1),
    ('document_max_mib', 1, 32, 'MiB', 'Source document', 1),
    ('document_max_pages', 40, 1000, 'pages', 'PDF pages', 1),
    ('collections_per_account', 5, 100, 'collections', 'Collections per account', 1),
    ('documents_per_collection', 20, 1000, 'documents', 'Documents per collection', 1),
    ('chunks_per_collection', 500, 50000, 'chunks', 'Chunks per collection', 1),
    ('chunks_per_source', 256, 10000, 'chunks', 'Chunks per source', 1),
    ('chunk_bytes', 1200, 16384, 'bytes', 'Chunk width', 32),
    ('chunk_overlap_bytes', 200, 8192, 'bytes', 'Chunk overlap', 0),
    ('extraction_max_mib', 5, 32, 'MiB', 'Extraction image upload', 1),
    ('audio_upload_mib', 32, 256, 'MiB', 'Audio upload', 1),
    ('image_max_megapixels', 32, 128, 'Mi pixels', 'Image pixels', 1),
    ('responses_max_records', 10000, 1000000, 'records', 'Stored Responses per account', 1),
    ('responses_max_mib', 256, 8192, 'MiB', 'Stored Responses per account size', 1),
):
    _add('weightroom', name, 'int', tier=NOW, scope='front', page='limits', section='Uploads and content limits',
         label=label, unit=unit, control='number', min=minimum, ceiling=ceiling, default=default,
         help=f'Applies to new work immediately. Groups may override this value; blank inherits. Ceiling: {ceiling} {unit}, except for superuser groups.')


_add('turnstile', 'allowance_mode', 'choice', tier=NOW, scope='front', page='allowances', section='Spending policy',
     label='Allowance policy', choices=('advisory', 'warn', 'block'), default='advisory',
     choice_labels=(('advisory', 'Advisory'), ('warn', 'Warn'), ('block', 'Block hosted requests')),
     help='Advisory reports recorded spending. Warn adds a response header to hosted requests and enables configured threshold notices. Block refuses hosted requests at an exhausted allowance; local requests continue. Unknown and in-flight costs are excluded.')
_add('turnstile', 'allowance_period', 'choice', tier=NOW, scope='front', page='allowances', section='Spending policy',
     label='Allowance period', choices=('rolling_24h', 'calendar_month'), default='rolling_24h',
     choice_labels=(('rolling_24h', 'Rolling 24 hours'), ('calendar_month', 'Calendar month')),
     help='Rolling 24 hours, or a calendar month in the control node time zone. Applies immediately to the same retained cost records. Short event retention may understate spending.')


_add('loadcoach', 'downloads_at_once', 'int', tier=NOW, scope='node', page='downloads', section='Downloads',
     label='Downloads at once', control='number', min=0, max=32, default=0,
     help='Maximum active file transfers on this node, shared by Hugging Face and Civitai. Zero preserves unlimited transfers. Lowering the limit lets active transfers finish; queued transfers read the current limit.')
for name, label, source, filename in (
    ('hub_token', 'Hugging Face token', 'hub_token_file', 'huggingface.token'),
    ('civitai_token', 'Civitai token', 'civitai_token_file', 'civitai.token'),
):
    _add('loadcoach', name, 'secret', tier=NOW, scope='node', page='downloads', section='Download credentials',
         label=label, default='', rule=_bearer_secret, credential_file=source, credential_default=filename,
         help='Write-only on the selected node. Blank keeps the current credential; explicit removal clears it. Read again for each provider operation.')



def _backup_time(value):
    """Validate the backup time setting before saving it."""
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('Use a backup time in HH:MM format.')


_add('weightroom', 'chat_keep_days', 'int', tier=NOW, scope='front', page='retention', section='Logs and retention',
     label='Conversation retention', unit='days', control='number', min=0, max=36500, default=0,
     help='Zero keeps conversations indefinitely. A daily sweep expires conversations by their last opening or new message, preserving active answers. Background polling does not renew activity. Applies at the next sweep without restarting.')
_add('weightroom', 'backup_every', 'choice', tier=NOW, scope='front', page='retention', section='Scheduled backups',
     label='Automatic control node backup', choices=('off', 'daily', 'weekly'), default='off',
     choice_labels=(('off', 'Off'), ('daily', 'Every day'), ('weekly', 'Every Sunday')),
     help='A built-in Schedules function creates and verifies the control node recovery archive. Weekly runs on Sunday. Other nodes retain their explicitly configured Schedules. Rotation uses Backups to keep. No node is woken.')
_add('weightroom', 'backup_time', 'text', tier=NOW, scope='front', page='retention', section='Scheduled backups',
     label='Automatic backup time', control='plain-text', default='03:00', rule=_backup_time,
     help='HH:MM in the configured control node time zone, or its host zone when empty. Changes recalculate the next future run. The latest missed run may catch up after downtime; overlapping runs are skipped.')


# Existing theme IDs; the color definitions remain in the shared theme picker.
PALETTES = ('slate', 'ocean', 'lagoon', 'forest', 'sage', 'clay', 'rose', 'orchid', 'indigo',
            'coffee', 'graphite', 'snow', 'sunflower', 'carbon-cyan', 'carbon-green', 'carbon-amber', 'carbon-violet')


def _site_name(value):
    """Validate the site name setting before saving it."""
    if not value.strip() or len(value) > 80 or any(ord(char) < 32 for char in value):
        raise ValueError('Use a site name from 1 to 80 characters on one line.')


def _chat_default(value):
    """Validate the chat default setting before saving it."""
    if len(value) > 160 or any(ord(char) < 32 for char in value):
        raise ValueError('Use a model or role name up to 160 characters on one line, or leave it empty.')


def banner_end(value):
    """Parse an optional explicitly zoned ISO time, avoiding ambiguous host-local clocks."""
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError('Banner end time must be an ISO date and time with a UTC offset, or empty.')
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError
        return at.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('Banner end time must be an ISO date and time with a UTC offset, or empty.') from None


def _site_banner(value):
    """Validate the site banner setting before saving it."""
    if value.keys() - {'text', 'level', 'ends_at'}:
        raise ValueError('The banner table accepts text, level and ends_at.')
    if not isinstance(value.get('text', ''), str) or len(value.get('text', '')) > 2000:
        raise ValueError('Banner text must be plain text up to 2,000 characters.')
    if value.get('level', 'info') not in ('info', 'warning', 'danger'):
        raise ValueError('Banner level must be info, warning or danger.')
    banner_end(value.get('ends_at', ''))


_add('weightroom', 'site_name', 'text', tier=NOW, scope='front', page='general', section='Display and costs',
     label='Site name', control='plain-text', default='openWeightGym', rule=_site_name,
     help='Name shown in the shared header, page titles and test notifications. Plain text, up to 80 characters. Runtime and service names stay unchanged.')
_add('weightroom', 'site_banner', 'table', tier=NOW, scope='front', page='general', section='Display and costs',
     label='Site announcement', default={'text':'', 'level':'info', 'ends_at':''}, rule=_site_banner,
     help='An announcement for everyone, including the sign-in page. Set text, level (info, warning or danger), and optional ends_at as an ISO date/time with an explicit UTC offset. Empty text hides it. Text is escaped, never HTML.')
_add('weightroom', 'default_palette', 'choice', tier=NOW, scope='front', page='general', section='Display and costs',
     label='Default color palette', choices=PALETTES, default='slate',
     choice_labels=tuple((name, name.replace('-', ' ').title()) for name in PALETTES),
     help='Starting palette for accounts without a saved theme in this browser. Existing account choices win. Use default in Appearance resumes inheritance.')
_add('weightroom', 'default_appearance', 'choice', tier=NOW, scope='front', page='general', section='Display and costs',
     label='Default appearance', choices=('system','light','dark'), default='system',
     choice_labels=(('system','Follow the device'),('light','Light'),('dark','Dark')),
     help='Starting appearance for accounts without a saved browser theme. Existing account choices win.')
_add('weightroom', 'default_chat_model', 'text', tier=NOW, scope='front', page='general', section='Display and costs',
     label='Default chat model or role', control='plain-text', default='', rule=_chat_default,
     help='Initial selection for a new text chat. Personal choices and explicit links win. Empty or unavailable names fall back to the first model or role allowed for that account; this never grants access.')



def _remote_name(value):
    """Validate the remote name setting before saving it."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value):
        raise ValueError('Remote server names use 1–64 lowercase letters, digits or dashes.')


def _remote_url(value):
    """Validate the remote url setting before saving it."""
    from urllib.parse import urlsplit
    try:
        url = urlsplit(value)
        valid = (len(value) <= 2048 and url.scheme in ('http', 'https') and url.hostname and
                 url.port != 0 and not url.username and not url.password and not url.query and not url.fragment and
                 not any(char.isspace() or ord(char) < 32 for char in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use an HTTP(S) MCP URL without embedded credentials, query parameters or fragments.')


def _remote_description(value):
    """Validate the remote description setting before saving it."""
    if len(value) > 2000:
        raise ValueError('Remote server descriptions may contain at most 2,000 characters.')


REMOTE_SERVER_FIELDS = (
    Setting('toollocker', 'name', 'text', label='Server name', control='plain-text', rule=_remote_name, default=''),
    Setting('toollocker', 'url', 'text', label='MCP URL', control='plain-text', rule=_remote_url, default=''),
    Setting('toollocker', 'description', 'text', label='Description', control='plain-text', rule=_remote_description, default=''),
    Setting('toollocker', 'level', 'choice', label='Sensitivity', choices=('normal','sensitive','restricted'), default='normal'),
    Setting('toollocker', 'public', 'bool', label='Public HTTPS server (no key)', default=False),
)


def _remote_servers(value):
    """Validate the remote servers setting before saving it."""
    if len(value) > 32:
        raise ValueError('At most 32 site-managed remote MCP servers are supported.')
    fields = {entry.name:entry for entry in REMOTE_SERVER_FIELDS}
    for name, server in value.items():
        fields['name'].check(name)
        if not isinstance(server, dict) or server.keys() - (fields.keys() - {'name'}):
            raise ValueError('Remote MCP servers accept URL, description, sensitivity and public mode; commands and key paths stay file-only.')
        for field in REMOTE_SERVER_FIELDS[1:]:
            field.check(server.get(field.name, field.default))
        if server.get('public',False) and not server['url'].startswith('https://'):
            raise ValueError('Public MCP servers require HTTPS.')


_add('toollocker', 'remote_servers', 'table', tier=NOW, scope='front', page='tools', section='Tools',
     label='Remote MCP servers', default={}, rule=_remote_servers, permission='superuser',
     help='Superusers can manage URL servers on the control node. File servers and built-in tools win on name clashes. Private servers need a write-only key; public servers require HTTPS. Commands remain file-only. Existing calls finish when a server is changed or removed.')
_add('toollocker', 'remote_server_key', 'secret', tier=NOW, scope='front', page='tools', section='Tools',
     label='Remote MCP server key', default='', rule=_bearer_secret, permission='superuser',
     help='Write-only bearer key for the named site-managed remote server. Blank keeps the saved key. Explicit removal clears it. Never included in history, export or reported settings.')
