"""Edit voice profiles as scalar fields while preserving each profile and setting state."""

from copy import deepcopy

from loadcoach.settings import read_value, wrong_kind
from plugins.hermes.reasoning import reasoning_levels

from voice.speakers import choices as available_speakers
from voice.prompt_recordings import TEXTS, texts
from voice.telephone_settings import validate as validate_telephone

DESCRIPTIONS = {
    "model": "Direct-mode model, requested with the caller's account permissions. Hermes has separate settings under voice-gateway.",
    "modes": "Allowed conversation modes for this voice profile.",
    "system_prompt": "Instructions for direct voice conversations.",
    "max_output_tokens": "Maximum tokens in one direct-mode answer.",
    "max_context_tokens": "Maximum direct-mode conversation context in tokens.",
    "speaker": "Speaker used for both direct and Hermes replies and greetings.",
}
PREFIX = "profiles.default."
GREETING_PREFIX = 'greetings.'
HERMES_FIELDS = {"hermes_model", "hermes_system_prompt", "hermes_max_turns", "hermes_context_tokens",
                 "hermes_max_output_tokens", "hermes_reasoning_effort"}


def holds(setting):
    """Use the saved value even while the voice service is still taking it up."""
    return setting["changed"]["value"] if setting.get("changed") else setting["value"]


def speaker_choices(settings):
    """Use names only for the installed bundle they describe; never guess another release's IDs."""
    synthesis = next((entry["value"] for entry in settings if entry["name"] == "synthesis"), {})
    return available_speakers(synthesis)


def fields(settings, models=()):
    """Replace the profile table with editable scalar rows, including each field's reset state."""
    result = []
    for entry in settings:
        if entry['name'] == 'greetings':
            for name, value in texts({'greetings': holds(entry)}).items():
                row = dict(name=GREETING_PREFIX + name, label='Greeting: ' + name, editor='text', multiline=True,
                           value=entry['value'].get(name, TEXTS[name]), file=entry.get('file', {}).get(name, TEXTS[name]),
                           when=entry['when'], says='1–300 characters. Used by the gateway while connecting or offline.',
                           refused=entry.get('refused'), changed=None)
                if entry.get('changed'):
                    row['changed'] = {**entry['changed'], 'value': value}
                result.append(row)
            continue
        if entry["name"] != "profiles":
            result.append(entry)
            continue
        for profile_id in holds(entry):
            current = entry["value"].get(profile_id, {})
            original = entry.get("file", {}).get(profile_id, {})
            wanted = holds(entry)[profile_id]
            for name, value in wanted.items():
                label = "Direct " + name.replace("_", " ") if name in ("model", "system_prompt", "max_output_tokens", "max_context_tokens") else name
                row = dict(name="profiles." + profile_id + "." + name, label=label, value=current.get(name, value),
                           when=entry["when"], says=DESCRIPTIONS.get(name, ""), changed=None,
                           refused=entry.get("refused"))
                if name in original:
                    row["file"] = original[name]
                if entry.get("changed") and (name not in original or value != original[name] or value != current.get(name)):
                    row["changed"] = {**entry["changed"], "value": value}
                if isinstance(value, str):
                    row["editor"] = "text"
                    row["multiline"] = name == "system_prompt"
                if name == "model":
                    row["choices"] = [(model, model) for model in models]
                    row["select"] = True
                elif name == "speaker":
                    row["choices"] = speaker_choices(settings)
                    if not row["choices"]:
                        row["says"] += " Speaker names are unavailable for this synthesis bundle."
                elif name == "modes":
                    row["choices"] = [('["direct"]', "Direct"), ('["hermes"]', "Hermes"),
                                      ('["direct", "hermes"]', "Direct and Hermes")]
                result.append(row)
    return result


def greeting_change(settings, form):
    """Merge greeting edits and resets without changing another phrase."""
    entry = next((row for row in settings if row['name'] == 'greetings'), None)
    if not entry or entry['when'] == 'file':
        return {}
    wanted = texts({'greetings': holds(entry)})
    for name in TEXTS:
        field = GREETING_PREFIX + name
        if form.get('reset') == field:
            wanted[name] = entry.get('file', {}).get(name, TEXTS[name])
        elif not form.get('reset') and 'value-' + field in form:
            wanted[name] = form['value-' + field]
    texts({'greetings': wanted})
    if wanted == holds(entry):
        return {}
    return {'greetings': None if wanted == entry.get('file') else wanted}


def profile_change(settings, form, models=()):
    """Merge submitted profile fields without overwriting unedited fields or other profiles."""
    entry = next((entry for entry in settings if entry["name"] == "profiles"), None)
    if not entry or entry["when"] == "file":
        return {}
    profiles = deepcopy(holds(entry))
    reset = form.get("reset", "")
    for row in fields(settings, models):
        field = row["name"]
        if not field.startswith("profiles."):
            continue
        _, profile_id, name = field.split(".", 2)
        profile = profiles[profile_id]
        if reset:
            if reset == field:
                if "file" in row:
                    profile[name] = row["file"]
                else:
                    profile.pop(name, None)
            continue
        if "value-" + field not in form:
            continue
        text = form["value-" + field]
        try:
            value = text if row.get("editor") == "text" else read_value(text, isinstance(profile[name], dict))
            if why := wrong_kind(value, profile[name]):
                raise ValueError(why)
            if name == "model" and (not value.strip() or (value != profile[name] and value not in models)):
                raise ValueError("Choose a model available to your account.")
            if name == "modes" and (not value or any(mode not in ("direct", "hermes") for mode in value)):
                raise ValueError("Choose direct, hermes, or both.")
            if name in ("max_output_tokens", "max_context_tokens") and value < 1:
                raise ValueError("Use at least one token.")
            if name == "speaker" and row.get("choices") and value not in dict(row["choices"]):
                raise ValueError("Choose a speaker from the installed bundle.")
            profile[name] = value
        except ValueError as why:
            raise ValueError(f"{name}: {why}") from None
    if profiles == holds(entry):
        return {}
    return {"profiles": None if profiles == entry.get("file") else profiles}


def gateway_fields(settings, models=()):
    """Show the independently authenticated Hermes agent's choices as individual controls."""
    machine = next((holds(row) for row in settings if row["name"] == "hermes"), {})
    result = []
    for entry in settings:
        row = dict(entry)
        name = row["name"]
        if name in HERMES_FIELDS:
            row["label"] = "Hermes " + name.removeprefix("hermes_").replace("_", " ")
            if isinstance(holds(row), str):
                row["editor"] = "text"
            if name == "hermes_model":
                row["select"] = True
                row["choices"] = [("", "Gateway default — " + machine.get("model", "not configured"))] + [(model, model) for model in models]
                row["says"] = "Hermes uses its own provider API key, independently of the caller's direct model. Applies to new calls."
            elif name == "hermes_system_prompt":
                row["multiline"] = True
            elif name == "hermes_reasoning_effort":
                levels = reasoning_levels()
                row["choices"] = [(effort, effort) for effort in levels or ()]
                if levels is None:
                    row["says"] = "Hermes reasoning choices are unavailable; the current setting is preserved."
                elif not levels:
                    row["says"] = "The Hermes schema declares no reasoning overrides."
                if holds(row) not in dict(row["choices"]):
                    row["choices"].append((holds(row), str(holds(row)) + " (current; not verified)"))
        result.append(row)
    return result


def gateway_change(settings, form, models=()):
    """Validate editable Hermes choices; machine paths, keys and tool permissions stay read-only."""
    if form.get("reset"):
        return {}
    changed = {}
    for row in gateway_fields(settings, models):
        name = row["name"]
        if name not in HERMES_FIELDS or row["when"] == "file" or "value-" + name not in form:
            continue
        text = form["value-" + name]
        try:
            value = text if row.get("editor") == "text" else read_value(text, False)
            old = holds(row)
            if why := wrong_kind(value, old):
                raise ValueError(why)
            if name == "hermes_model" and value and value != old and value not in models:
                raise ValueError("Choose a model available to your account.")
            if name == "hermes_reasoning_effort" and value not in dict(row["choices"]):
                raise ValueError("Choose a listed reasoning effort.")
            if name in ("hermes_max_turns", "hermes_context_tokens") and value < 1:
                raise ValueError("Use at least one.")
            if value != old:
                changed[name] = value
        except ValueError as why:
            raise ValueError(f"{name}: {why}") from None
    return changed


def workspace(programs):
    """Arrange existing setting rows into independently saved task cards, without copying controls."""
    profiles, shared = {}, []
    for program in programs:
        for row in program['settings']:
            if row['when'] == 'file' or row['name'] in ('telephone', 'public_host', 'public_port'):
                continue
            name = row['name']
            if name.startswith('profiles.'):
                profile = name.split('.', 2)[1]
                title = 'Voice and greetings' if name.endswith('.speaker') else 'Conversation'
                bucket = profiles.setdefault(profile, {})
            else:
                bucket = None
                title = ('Voice and greetings' if name.startswith('greetings.') else
                         'Recognition' if name.startswith('recognition.') else
                         'Synthesis' if name.startswith('synthesis.') else
                         'Hermes' if name in HERMES_FIELDS or name.startswith('hermes.') else 'Capacity')
            target = bucket.setdefault(title, []) if bucket is not None else shared
            target.append((title, program, row))
    # Greetings are global; keep their single editor alongside the default profile's speaker.
    primary = 'default' if 'default' in profiles else next(iter(profiles), None)
    if primary:
        greetings = [entry for entry in shared if entry[2]['name'].startswith('greetings.')]
        profiles[primary].setdefault('Voice and greetings', []).extend(greetings)
        shared = [entry for entry in shared if entry not in greetings]
    def groups(rows, profile=None):
        result = []
        for title in ('Conversation', 'Voice and greetings', 'Recognition', 'Synthesis', 'Hermes', 'Capacity'):
            entries = []
            for program in programs:
                settings = [row for heading, owner, row in rows if heading == title and owner is program]
                if settings:
                    entries.append({**program, 'settings': settings})
            if entries:
                result.append({'title': title, 'programs': entries, 'open': True,
                               **({'key':'Speech', 'save_title':'Speech'} if title == 'Voice and greetings' and profile == primary else {})})
        return result
    return {'profiles': [{'id': name, 'groups': groups([row for rows in buckets.values() for row in rows], name)}
                         for name, buckets in profiles.items()], 'shared': groups(shared)}
