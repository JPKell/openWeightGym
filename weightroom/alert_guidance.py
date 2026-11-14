"""Investigation routes and detection methods for current and retained alert episodes."""
from urllib.parse import urlencode

from loadcoach.observation_store import SOURCE_FRESH_SECONDS
from weightroom.alerts import OBSERVATION_BACKLOG_SECONDS


def step(label, href, text, ability=''):
    return dict(label=label, href=href, text=text, ability=ability)


def investigation(row):
    """Add guidance without replacing the episode's recorded evidence or inferring its cause."""
    source, condition = row['source'], row.get('condition', '')
    if source == 'observability' and condition.startswith('observation:'):
        name, _, rule = condition.removeprefix('observation:').rpartition(':')
        if not name or rule not in ('freshness', 'backlog', 'loss', 'error-rate', 'latency'):
            return None
        local = name == 'front'
        measured = rule in ('error-rate', 'latency')
        metrics = '/requests?' + urlencode({'observe': 1, 'complete': 1, **({'service': name} if measured else {'machine': name})}) + '#observation-metrics'
        steps = [step('Model requests → Operational metrics', metrics,
                      (f'Inspect service {name}, its ingress sample count, error rate and approximate p95.' if measured else
                       f'Find the {name} source; compare freshness, recording, last receipt, forwarding backlog and dropped-record counters.'), 'see-log')]
        commands = []
        if measured:
            services = f'The instrumented {name} service; Turnstile on front retains the ingress metrics; WeightRoom on front checks the thresholds.'
            detected = ('WeightRoom reads GET /loadcoach/observation-metrics from front’s Turnstile every 30 seconds. '
                        'Configured thresholds apply to retained ingress metric windows with a minimum sample count and a sustained hold period. '
                        'The recorded explanation contains the measurement and threshold used for this episode.')
            steps.append(step('System logs', '/output', f'Select the host and configured {name} service. Inspect errors and slow operations around the episode timestamps.', 'restart-services'))
            steps.append(step('Observation alert settings', '/settings?open=weightroom', 'Check the measured window, minimum samples, hold period and configured error/p95 thresholds.'))
        else:
            services = ('Turnstile’s local observation writer on front; WeightRoom’s alert collector on front.' if local else
                        f'LoadCoach on node {name} (normally loadcoach.service); Turnstile on front (normally turnstile.service); '
                        'WeightRoom’s alert collector on front (normally weightroom.service).')
            detected = ('Every 30 seconds WeightRoom reads GET /loadcoach/observation-metrics from front’s Turnstile. '
                        + ('For front, source availability means that report contains local writer health.' if local else
                           f'LoadCoach forwards its health to front through signed POST /turnstile/log, normally about every 10 seconds while running. '
                           f'Front marks {name} fresh only when the last health receipt is less than {SOURCE_FRESH_SECONDS} seconds old and recording=true. '
                           'This observes the reporting pipeline; it is not a direct systemd status check.'))
            if rule == 'backlog':
                detected += f' Backlog alerts require fresh evidence, a nonempty forwarding queue and an oldest unforwarded observation at least {OBSERVATION_BACKLOG_SECONDS} seconds old.'
            elif rule == 'loss':
                detected += ' Loss alerts follow increases in process-lifetime drop counters during the configured hold period; successful sampling is excluded and a replaced process starts a new comparison baseline.'
            if not local:
                steps.append(step('Compute nodes', '/compute-nodes', f'Find {name}; check whether that name still identifies an active node, whether it is intentionally asleep/stopped, and its connectivity and machine-key status. Alert collection never wakes it.'))
                steps.append(step('System logs → LoadCoach', '/output?' + urlencode({'node': name, 'source': 'loadcoach'}),
                                  f'Inspect LoadCoach on {name} for observation-writer, storage or connection errors around the last health receipt. Forwarding retries can be silent.', 'restart-services'))
                commands.append(dict(host=f'The host running node {name}', command='journalctl --user -u loadcoach.service --since "15 minutes ago" --no-pager'))
            steps.append(step('Components', '/system/components', 'Check the installed service versions. On front, inspect Turnstile’s journal for signed /turnstile/log receipt, authentication or database failures. If the metrics endpoint itself is unavailable, inspect WeightRoom’s collector and its connection to Turnstile.'))
            commands.append(dict(host='Front', command='journalctl --user -u turnstile.service -u weightroom.service --since "15 minutes ago" --no-pager'))
        return dict(detection=detected, services=services, steps=steps, commands=commands)
    if source == 'services':
        detection = 'Spotter reads systemd ActiveState and Result through /spotter/alert-evidence; WeightRoom checks that sanitized evidence every 30 seconds. ActiveState=failed opens this episode. Unknown readings cannot prove recovery.'
        services = 'The unit named in the recorded explanation; Spotter on its host supplies the systemd reading; WeightRoom on front records the episode.'
        steps = [step('System logs', '/output', 'Select the node and the failed service named above; inspect its journal around first/last observation.', 'restart-services'),
                 step('Telemetry', '/telemetry', 'Open the affected host and check its service/component state and available memory.')]
    elif source == 'GPU temperature':
        detection = 'Spotter reads numeric GPU temperatures and WeightRoom compares them with the configured GPU alert threshold every 30 seconds. A fresh numeric reading below the threshold establishes recovery; a missing reading does not.'
        services = 'Spotter and the GPU driver on the affected node; WeightRoom on front applies the threshold. This alert does not shut down a GPU.'
        steps = [step('Telemetry', '/telemetry', 'Open the affected host; compare the GPU number, temperature, power and current model load.'),
                 step('GPU alert threshold', '/settings?open=weightroom', 'Check the configured GPU temperature threshold.'),
                 step('System logs → kernel', '/output?source=kernel', 'Select the affected node; inspect GPU/driver faults near the episode timestamps.', 'restart-services')]
    elif source in ('memory kills', 'kernel journal'):
        detection = ('Spotter reads systemd Result=oom-kill or up to 100 matching kernel journal events from the last 24 hours through /spotter/alert-evidence. '
                     'Unit/invocation IDs or journal cursors deduplicate incidents. Kernel access is checked separately; missing journal access is not proof of a clean system.')
        services = 'The unit named in the evidence, if recorded; Spotter and the kernel journal on that host; WeightRoom on front retains the incident.'
        steps = [step('System logs → kernel', '/output?source=kernel', 'Select the affected node and inspect memory-kill events around the recorded incident time; also check the named service journal.', 'restart-services'),
                 step('Telemetry', '/telemetry', 'Inspect host RAM and GPU memory pressure, model placement and concurrent work. Mark reviewed only after investigating; review does not prove recovery.')]
    elif source == 'model failures':
        detection = 'Turnstile’s Reliability state excludes a specific model ID, fingerprint and role after repeated failed outcomes. WeightRoom reads that state through /loadcoach/alert-evidence; an ordinary successful request does not itself remove an exclusion.'
        services = 'Turnstile on front owns the exclusion; LoadCoach and the selected inference runtime execute the affected model.'
        steps = [step('Reliability', '/reliability', 'Find this model, fingerprint and role; inspect issue requests and the recorded exclusion reason.', 'see-log'),
                 step('Model requests', '/requests', 'Open the failed requests and their diagnostics; compare the exact model revision and node.', 'see-log'),
                 step('System logs', '/output', 'Inspect LoadCoach and the relevant model runtime on the affected node.', 'restart-services')]
    elif source == 'spending':
        detection = 'Turnstile compares retained, recorded 24-hour provider/account costs with configured ceilings through /loadcoach/alert-evidence. In-flight requests and unknown costs are excluded; no future-cost reservation is inferred.'
        services = 'Turnstile on front owns the provider accounts, user/group ceilings and retained cost totals; WeightRoom records the alert.'
        steps = [step('Usage', '/usage', 'Compare recorded provider and user costs for the affected 24-hour period.'),
                 step('Cloud APIs', '/cloud-apis', 'For a provider alert, inspect the named account’s spending ceiling.'),
                 step('Users and groups', '/users', 'For a user alert, inspect that account’s group spending limit.', 'manage-users')]
    else:
        return None
    return dict(detection=detection, services=services, steps=steps, commands=[])
