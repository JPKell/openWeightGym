"""Recognize integrated graphics without allocating GPU memory or starting an engine."""

import ctypes
import os
from pathlib import Path
import re
import struct

# Public Linux DRM uAPIs: include/uapi/drm/{amdgpu,i915,xe}_drm.h.
# Keep device ordinals unchanged: classification never renumbers driver devices.
INTEGRATED_NAMES = re.compile(
    r'\bintegrated\b|\biGPU\b|\bAPU\b|\bRadeon(?:\(TM\))?\s+(?:Graphics|[3-9]\d{2}M)\b|'
    r'\b(?:Raphael|Raven Ridge|Renoir|Cezanne|Rembrandt|Phoenix|Mendocino|Strix Point|Strix Halo)\b|'
    r'\bIntel.*(?:\bUHD\b|\bHD Graphics\b|\bIris\b(?!.*\bMAX\b))|'
    r'\b(?:Tegra|Jetson|NVIDIA GB10)\b', re.I)


def is_integrated(device):
    """Use producer metadata first, then positive identities in retained/legacy reports."""
    if device.get('integrated') is not None:
        return device['integrated'] is True
    kind = str(device.get('device_type', '')).lower()
    if kind in ('integrated', 'integrated_gpu', 'cpu'):
        return True
    if kind in ('discrete', 'discrete_gpu'):
        return False
    # Raphael's display function remains identifiable with its driver unbound.
    vendor = str(device.get('vendor', '')).lower()
    raw_id = device.get('device_id', device.get('device', ''))
    device_id = f'{raw_id:x}' if isinstance(raw_id, int) else str(raw_id).lower().removeprefix('0x')
    amd = vendor in ('amd', '0x1002', '1002', '4098')
    if amd and device_id == '164e':
        return True
    name = ' '.join(str(device.get(key) or '') for key in ('description', 'model', 'name'))
    return bool(INTEGRATED_NAMES.search(name) or
                (amd or re.search(r'\bAMD\b', name, re.I)) and re.search(r'\b0x164e\b', name, re.I))


def _ioctl(fd, number, request, direction=3):
    """Issue an information-only DRM query using the Linux generic ioctl encoding."""
    import fcntl
    return fcntl.ioctl(fd, (direction << 30) | (len(request) << 16) | (ord('d') << 8) | number, request)


def _driver_integrated(fd, driver):
    """Read AMD's Fusion bit or Intel's device-local memory configuration."""
    if driver == 'amdgpu':
        # AMDGPU_INFO_DEV_INFO: request only the stable prefix through ids_flags.
        info = ctypes.create_string_buffer(144)
        _ioctl(fd, 0x45, struct.pack('=QII16x', ctypes.addressof(info), len(info), 0x16), 1)
        return bool(struct.unpack_from('=Q', info, 136)[0] & 1)  # AMDGPU_IDS_FLAGS_FUSION
    if driver == 'xe':
        query = bytearray(struct.pack('=QIIQQQ', 0, 2, 0, 0, 0, 0))
        _ioctl(fd, 0x40, query)
        size = struct.unpack_from('=I', query, 12)[0]
        if not 24 <= size <= 65536:
            return None
        info = ctypes.create_string_buffer(size)
        struct.pack_into('=Q', query, 16, ctypes.addressof(info))
        _ioctl(fd, 0x40, query)
        if struct.unpack_from('=I', info)[0] < 2:
            return None
        return not bool(struct.unpack_from('=Q', info, 16)[0] & 1)  # CONFIG_FLAG_HAS_VRAM
    if driver == 'i915':
        item = ctypes.create_string_buffer(struct.pack('=QiIQ', 4, 0, 0, 0), 24)
        query = struct.pack('=IIQ', 1, 0, ctypes.addressof(item))
        _ioctl(fd, 0x79, query)
        size = struct.unpack_from('=i', item, 8)[0]
        if not 16 <= size <= 65536:
            return None
        info = ctypes.create_string_buffer(size)
        struct.pack_into('=Q', item, 16, ctypes.addressof(info))
        _ioctl(fd, 0x79, query)
        count = struct.unpack_from('=I', info)[0]
        if not count or 16 + count * 88 > size:
            return None
        return not any(struct.unpack_from('=H', info, 16 + index * 88)[0] == 1
                       for index in range(count))  # I915_MEMORY_CLASS_DEVICE
    return None


def pci_integrated(path, driver):
    """Classify the exact PCI function; missing permissions/drivers remain unknown."""
    if driver not in ('amdgpu', 'i915', 'xe'):
        return None
    for render in sorted((path / 'drm').glob('renderD*')):
        try:
            fd = os.open('/dev/dri/' + render.name, os.O_RDONLY | os.O_CLOEXEC)
            try:
                return _driver_integrated(fd, driver)
            finally:
                os.close(fd)
        except (OSError, ValueError, struct.error):
            continue
    return None


if __name__ == '__main__':
    # The installer runs before the suite/its Python environment is installed.
    import sys
    if sys.argv[1] == '--name':
        print('integrated' if is_integrated({'name': sys.argv[2]}) else 'unknown')
        raise SystemExit(0)
    path = Path(sys.argv[1]).resolve()
    driver = (path / 'driver').resolve().name if (path / 'driver').exists() else ''
    integrated = pci_integrated(path, driver)
    metadata = {'vendor': (path / 'vendor').read_text().strip(),
                'device_id': (path / 'device').read_text().strip(), 'integrated': integrated}
    print('integrated' if is_integrated(metadata) else 'discrete' if integrated is False else 'unknown')
