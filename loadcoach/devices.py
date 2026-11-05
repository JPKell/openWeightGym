"""Node GPU inventory and allocation policy, independent of installed model engines."""

import csv
import os
from pathlib import Path
import re
import subprocess

VENDORS = {0x10de: 'NVIDIA', 0x1002: 'AMD', 0x8086: 'Intel'}
PCI_ROOT = Path('/sys/bus/pci/devices')


def read(path):
    """Read optional sysfs metadata without making missing driver files fatal."""
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def query(command):
    """Optional driver utilities must not prevent the compute service from starting."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=5)
        return result.stdout if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def pci_address(value):
    """Normalize NVIDIA's eight-digit PCI domain to Linux's four-digit domain."""
    match = re.search(r'([0-9a-fA-F]+):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}\.[0-7])$', value)
    return f'{int(match[1], 16):04x}:{match[2].lower()}:{match[3].lower()}' if match else ''


def hardware(pci_root=None, nvidia_smi='nvidia-smi'):
    """Find NVIDIA, AMD and Intel display/accelerator devices even without user-space drivers."""
    found = {}
    for path in sorted((pci_root if pci_root is not None else PCI_ROOT).glob('*')):
        try:
            vendor = VENDORS.get(int(read(path / 'vendor'), 16))
            device_class = int(read(path / 'class'), 16) >> 16
        except ValueError:
            continue
        driver = (path / 'driver').resolve().name if (path / 'driver').exists() else ''
        if not vendor or (device_class != 0x03 and not
                          (device_class == 0x12 and driver in ('nvidia', 'amdgpu', 'i915', 'xe'))):
            continue
        pci = pci_address(path.name)
        if not pci:
            continue
        description = read(path / 'product_name') or f'{vendor} GPU ({read(path / "device")})'
        found[pci] = {'id': f'{vendor}@{pci}', 'vendor': vendor, 'pci': pci,
                      'description': description, 'driver': driver}
    # nvidia-smi works on headless systems without DRM nodes and supplies useful names/UUIDs.
    printed = query([nvidia_smi, '--query-gpu=index,pci.bus_id,name,uuid', '--format=csv,noheader,nounits'])
    for row in csv.reader(printed.splitlines()):
        if len(row) != 4:
            continue
        index, bus, name, uuid = (value.strip() for value in row)
        pci = pci_address(bus)
        if not pci or not index.isdigit():
            continue
        found.setdefault(pci, {'vendor': 'NVIDIA', 'pci': pci, 'driver': 'nvidia'})
        found[pci].update(description=name, uuid=uuid, smi_index=int(index))
    # Preserve existing CUDA names. CUDA_DEVICE_ORDER=PCI_BUS_ID fixes the same ordering.
    ordinal = 0
    for device in sorted(found.values(), key=lambda one: one['pci']):
        if device['vendor'] == 'NVIDIA' and ('smi_index' in device or device['driver'] == 'nvidia'):
            device.update(id=f'CUDA{ordinal}', cuda_index=ordinal)
            ordinal += 1
    return sorted(found.values(), key=lambda one: one['pci'])


def discover(llama_server='', *, nvidia_smi='nvidia-smi'):
    """Hardware is authoritative; an optional llama device list only supplies backend aliases."""
    found = hardware(nvidia_smi=nvidia_smi)
    if llama_server:
        from engines.llama_cpp.devices import detect
        aliases = detect(llama_server)
        if not found:
            return aliases  # legacy/custom devices and non-PCI backends remain supported
        # Bind non-CUDA aliases only when the physical PCI address is explicitly reported.
        # Matching an ordinal or a model name can silently select a different identical card.
        for device in found:
            matches = [alias for alias in aliases if device['pci'] in alias['description'].lower()]
            if len(matches) == 1:
                device['llama_device'] = matches[0]['id']
    return found


def compatible(config, engine, card):
    """Detection does not imply that every installed engine supports a vendor/backend."""
    device = next((one for one in config.gpu_details if one['id'] == card), {})
    if not device.get('vendor'):
        return True  # explicitly configured legacy device names
    if engine == 'llama_cpp' and device.get('llama_device'):
        return True
    return device['vendor'] == 'NVIDIA' and 'cuda_index' in device


def allowed(config, engine):
    """One policy for model admission, benchmarks, reloads and native launches."""
    assigned = config.engine_gpus.get(engine)
    return tuple(card for card in config.gpus if not disabled(config, card)
                 and (assigned is None or card in assigned or identity(config, card) in assigned)
                 and compatible(config, engine, card))


def identity(config, card):
    """Persist assignments by physical identity while retaining existing CUDA display names."""
    device = next((one for one in config.gpu_details if one['id'] == card), {})
    return device.get('uuid') or (f"{device['vendor']}@{device['pci']}" if device.get('pci') else card)


def disabled(config, card):
    """Accept physical identities and legacy device names for node-wide exclusions."""
    return card in config.disabled_gpus or identity(config, card) in config.disabled_gpus


def cuda_environment(config, cards):
    """Expose exactly the assigned physical CUDA cards; local CUDA ordinals start at zero."""
    selectors = []
    for card in cards:
        device = next((one for one in config.gpu_details if one['id'] == card), {})
        if device.get('vendor') and (device['vendor'] != 'NVIDIA' or 'cuda_index' not in device):
            raise ValueError(f'{card} is detected, but this engine requires a CUDA GPU.')
        match = re.fullmatch(r'(?:CUDA|GPU)(\d+)', card)
        if not match and 'cuda_index' not in device:
            raise ValueError(f'Cannot resolve the physical CUDA device for {card}.')
        selectors.append(device.get('uuid') or str(device.get('cuda_index', int(match[1]) if match else 0)))
    return {**os.environ, 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID', 'CUDA_VISIBLE_DEVICES': ','.join(selectors)}


def telemetry_index(config, card):
    """Never confuse an AMD/Intel card with NVIDIA device zero in memory accounting."""
    device = next((one for one in config.gpu_details if one['id'] == card), {})
    if device.get('vendor') and device['vendor'] != 'NVIDIA':
        return None
    match = re.fullmatch(r'(?:CUDA|GPU)(\d+)', card)
    return device.get('smi_index', int(match[1]) if match else None)
