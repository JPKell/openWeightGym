"""Driver-level workflow peaks for the owned image session, sampled every 100 ms."""
import asyncio
import ctypes
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace

from loadcoach.processes import ProcessGroup


class Meter:
    """Aggregate only owned PIDs; other resident models are never charged to this workflow."""
    class Process(ctypes.Structure):
        """Match the NVML per-process memory ABI."""
        _fields_=[('pid',ctypes.c_uint),('used',ctypes.c_ulonglong),
                  ('gpu_instance',ctypes.c_uint),('compute_instance',ctypes.c_uint)]

    def __init__(self,cards):
        """Initialize private image state without loading model weights."""
        self.handles=[]
        self.problem=None
        self.samples=0
        self.gap=0
        self.previous=None
        self.peaks=[0]*len(cards)
        self.rss=0
        self.stages={}
        self.stage='preparation'
        self.stopping=asyncio.Event()
        self.group=ProcessGroup(SimpleNamespace(pid=os.getpid(),returncode=None))
        if not cards:
            return
        try:
            self.lib=ctypes.CDLL('libnvidia-ml.so.1')
            self.check(self.lib.nvmlInit_v2())
            for card in cards:
                handle=ctypes.c_void_p()
                if card.startswith('GPU-'):
                    self.check(self.lib.nvmlDeviceGetHandleByUUID(card.encode(),ctypes.byref(handle)))
                else:
                    self.check(self.lib.nvmlDeviceGetHandleByIndex_v2(int(card.removeprefix('CUDA')),ctypes.byref(handle)))
                self.handles.append(handle)
        except Exception as problem:
            self.problem=type(problem).__name__

    @staticmethod
    def check(code):
        """Reject unavailable driver observations."""
        if code:
            raise ValueError('Driver measurement failed: '+str(code))

    def sample(self):
        """Measure per-card memory only for the owned image session."""
        members=self.group.members()
        if not members:
            raise ValueError('Image session ownership could not be observed')
        sizes=[]
        for handle in self.handles:
            count=ctypes.c_uint(256)
            processes=(self.Process*256)()
            self.check(self.lib.nvmlDeviceGetComputeRunningProcesses_v2(handle,ctypes.byref(count),processes))
            owned=[processes[i].used for i in range(count.value) if processes[i].pid in members]
            if any(value>=2**63 for value in owned):
                raise ValueError('GPU process usage is unavailable')
            sizes.append(math.ceil(sum(owned)/2**20))
        rss=0
        for pid in members:
            try:
                rss+=int(Path(f'/proc/{pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')/2**20
            except (OSError,IndexError):
                pass
        now=time.monotonic()
        if self.previous is not None:
            self.gap=max(self.gap,now-self.previous)
        self.previous=now
        self.samples+=1
        self.rss=max(self.rss,math.ceil(rss))
        self.peaks=[max(a,b) for a,b in zip(self.peaks,sizes)]
        before=self.stages.setdefault(self.stage,[0]*len(sizes))
        self.stages[self.stage]=[max(a,b) for a,b in zip(before,sizes)]

    async def run(self):
        """Sample until workflow completion or a driver observation failure."""
        while not self.stopping.is_set() and not self.problem:
            try:
                await asyncio.to_thread(self.sample)
            except Exception as problem:
                self.problem=type(problem).__name__
                return
            try:
                await asyncio.wait_for(self.stopping.wait(),.1)
            except TimeoutError:
                pass

    def result(self):
        """Return per-stage peaks with explicit observation completeness."""
        return {'gpu_peak_mib':self.peaks,'rss_peak_mib':self.rss,'samples':self.samples,
                'maximum_sample_gap_seconds':round(self.gap,3),'stages':self.stages,'problem':self.problem,
                'complete':not self.problem and self.samples>=2 and self.gap<=.5 and all(self.peaks)}


def envelope(asked,settings):
    """Only the largest configured generation, references, adapters and upscale qualify sharing."""
    from mirrorwall.model import limits,defaults
    maximum=limits(settings)
    options=settings['sdcpp']
    normal=defaults(settings)
    return (asked['width']==maximum['max_width'] and asked['height']==maximum['max_height'] and
            len(asked['references'])==maximum['max_references'] and
            asked['reference_megapixels']==options.get('reference_megapixels',1) and
            asked['upscale']==maximum['max_upscale'] and
            all(asked.get('loras',{}).get(name,entry.get('scale',1))!=0 for name,entry in options.get('loras',{}).items()) and
            all(asked[key]==normal[key] for key in ('sampler','scheduler','cfg_scale')))
