"""Owner-only provider credentials on the node, selected by catalogue file metadata."""
from pathlib import Path
from datetime import datetime, timezone

from loadcoach.setting_catalog import entries, get, validate
from voice.telephone_keys import atomic_secret


def contracts():
    """Return node download credential definitions in catalogue order."""
    return tuple(entry for entry in entries('loadcoach') if entry.credential_file)


class DownloadKeys:
    """Read provider files per use; public status contains only saved/time metadata."""
    def __init__(self, config):
        self.config = config

    def path(self, kind):
        """Honor configured provider paths, otherwise use the node's existing keys folder."""
        entry = get('loadcoach', kind) if isinstance(kind, str) else None
        if entry is None or not entry.credential_file:
            raise ValueError('Choose a catalogued download credential.')
        if selected := getattr(self.config, entry.credential_file, ''):
            return Path(selected).absolute()
        if self.config.machine_key_file:
            folder = Path(self.config.machine_key_file).resolve().parent
        elif self.config.path:
            folder = Path(self.config.path).resolve().parent.parent / 'keys'
        else:
            folder = Path(self.config.data).resolve() / 'keys'
        return folder / entry.credential_default

    def read(self, kind):
        """Return the current credential without logging it or changing its source file."""
        try:
            return validate('loadcoach', kind, self.path(kind).read_text().strip())
        except (OSError, ValueError, UnicodeError):
            return ''

    def status(self):
        """Expose only presence and change time, with a safe unavailable state."""
        result = {}
        for entry in contracts():
            try:
                changed = self.path(entry.name).stat().st_mtime
                result[entry.name] = {'saved': True, 'changed': datetime.fromtimestamp(changed, timezone.utc).isoformat()}
            except FileNotFoundError:
                result[entry.name] = {'saved': False, 'changed': None}
            except OSError:
                result[entry.name] = {'saved': False, 'changed': None, 'refused': 'Credential status is unavailable on this node.'}
        return result

    def save(self, kind, credential):
        """Validate through the catalogue and atomically replace nonempty credentials as 0600."""
        path = self.path(kind)
        validate('loadcoach', kind, credential)
        if credential:
            atomic_secret(path, credential)

    def remove(self, kind):
        """Explicitly remove only the selected provider credential."""
        self.path(kind).unlink(missing_ok=True)
