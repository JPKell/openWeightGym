"""Live global content limits and validated group overrides from the setting catalogue."""

from loadcoach.setting_catalog import entries, default_for, validate


def contracts():
    """Return the canonical public limits in display order."""
    return tuple(entry for entry in entries('weightroom') if entry.page == 'limits' or entry.page=='video' and entry.ceiling is not None)


def checked(values, *, superuser=False):
    """Validate only known limits, with the explicit group ceiling exemption."""
    if not isinstance(values, dict) or values.keys() - {entry.name for entry in contracts()}:
        raise ValueError('Content limits must be a table of catalogue limit names.')
    return {name: validate('weightroom', name, value, context={'superuser_limits': superuser})
            for name, value in values.items()}


class ContentLimits:
    """Read globals for every operation; caller policy comes from authenticated account state."""
    def __init__(self, read=None):
        """Bind a live policy reader, falling back to shipped content limits."""
        self.read = read or (lambda name: default_for('weightroom', name))

    def for_user(self, user=None):
        """Resolve group override or global value, clamping non-superuser ceilings."""
        info = user if isinstance(user, dict) else getattr(user, 'info', getattr(user, 'me', {}))
        group = getattr(user, 'group', None)
        if group is not None and not isinstance(group, str):
            overrides, can = getattr(group, 'limits', {}), group.can
        else:
            overrides, can = info.get('limits', {}), info.get('can', ())
        result = {}
        for entry in contracts():
            value = overrides.get(entry.name, self.read(entry.name))
            value = validate('weightroom', entry.name, value, context={'superuser_limits': True})
            result[entry.name] = value if 'superuser' in can else min(value, entry.ceiling)
        return result


def value(limits, name):
    """Use catalogue defaults for library callers without an account context."""
    return (limits or {}).get(name, default_for('weightroom', name))


def body_bytes(limits, name, overhead=65536):
    """Bound base64 JSON transports using the effective decoded upload size."""
    return (value(limits, name) * 1024 * 1024 + 2) // 3 * 4 + overhead
